//! MultiPaxos -- client request entrance.

use super::*;

use crate::server::{ApiReply, ApiRequest, Command, LogAction};
use crate::utils::{Bitmap, SummersetError};

// MultiPaxosReplica client requests entrance
impl MultiPaxosReplica {
    /// Handler of client request batch chan recv.
    pub(super) async fn handle_req_batch(
        &mut self,
        mut req_batch: ReqBatch,
    ) -> Result<(), SummersetError> {
        let batch_size = req_batch.len();
        debug_assert!(batch_size > 0);
        pf_debug!("got request batch of size {}", batch_size);

        // if I'm not a prepared leader, ignore client requests
        if !self.is_leader() || self.bal_prepared == 0 {
            for (client, req) in req_batch {
                if let ApiRequest::Req { id: req_id, .. } = req {
                    // tell the client to try on known leader or just the
                    // next ID replica
                    let target = if let Some(peer) = self.leader {
                        peer
                    } else {
                        (self.id + 1) % self.population
                    };
                    self.external_api.send_reply(
                        ApiReply::Reply {
                            id: req_id,
                            result: None,
                            redirect: Some(target),
                        },
                        client,
                    )?;
                    pf_trace!(
                        "redirected client {} to replica {}",
                        client,
                        target
                    );
                }
            }
            return Ok(());
        }

        // if I'm a majority-leased leader or if simulating read leases, extract
        // all the reads and immediately reply to them
        if (self.is_leader()
            && self.bal_max_seen == self.bal_prepared
            && self.bal_prepared > 0
            && self.lease_manager.lease_cnt() + 1 >= self.quorum_cnt)
           // NOTE: this is only for benchmarking purposes
           || self.config.sim_read_lease
        {
            for (client, req) in &req_batch {
                if let ApiRequest::Req {
                    id: req_id,
                    cmd: Command::Get { key },
                } = req
                {
                    // has to use the `do_sync_cmd()` API
                    let (old_results, cmd_result) = self
                        .state_machine
                        .do_sync_cmd(*req_id, Command::Get { key: key.clone() })
                        .await?;
                    for (old_id, old_result) in old_results {
                        self.handle_cmd_result(old_id, old_result).await?;
                    }

                    self.external_api.send_reply(
                        ApiReply::Reply {
                            id: *req_id,
                            result: Some(cmd_result),
                            redirect: None,
                        },
                        *client,
                    )?;
                    pf_trace!("replied -> client {} for read-only cmd", client);
                }
            }

            req_batch.retain(|(_, req)| {
                !matches!(
                    req,
                    ApiRequest::Req {
                        cmd: Command::Get { .. },
                        ..
                    }
                )
            });
            if req_batch.is_empty() {
                return Ok(());
            }
        }

        // create a new instance in the first null slot (or append a new one
        // at the end if no holes exist); fill it up with incoming data
        let slot = self.first_null_slot();
        {
            let inst = &mut self.insts[slot - self.start_slot];
            debug_assert_eq!(inst.status, Status::Null);
            inst.reqs.clone_from(&req_batch);
            inst.leader_bk = Some(LeaderBookkeeping {
                trigger_slot: 0,
                endprep_slot: 0,
                prepare_acks: Bitmap::new(self.population, false),
                prepare_max_bal: 0,
                accept_acks: Bitmap::new(self.population, false),
            });
            inst.external = true;
        }

        // start the Accept phase for this instance
        let inst = &mut self.insts[slot - self.start_slot];
        inst.bal = self.bal_prepared;
        inst.status = Status::Accepting;
        pf_debug!("enter Accept phase for slot {} bal {}", slot, inst.bal);

        // [for perf breakdown]
        if let Some(sw) = self.bd_stopwatch.as_mut() {
            sw.record_now(slot, 0, None)?;
        }

        // record update to largest accepted ballot and corresponding data
        inst.voted = (inst.bal, req_batch.clone());
        self.storage_hub.submit_action(
            Self::make_log_action_id(slot, Status::Accepting),
            LogAction::Append {
                entry: WalEntry::AcceptData {
                    slot,
                    ballot: inst.bal,
                    reqs: req_batch.clone(),
                },
                sync: self.config.logger_sync,
            },
        )?;
        pf_trace!(
            "submitted AcceptData log action for slot {} bal {}",
            slot,
            inst.bal
        );

        // send Accept messages to all peers
        self.transport_hub.bcast_msg(
            PeerMsg::Accept {
                slot,
                ballot: inst.bal,
                reqs: req_batch,
            },
            None,
        )?;
        pf_trace!(
            "broadcast Accept messages for slot {} bal {}",
            slot,
            inst.bal
        );

        Ok(())
    }

    /// [for stale read profiling]
    pub(super) fn val_ver_of_first_key(
        &mut self,
    ) -> Result<Option<(String, usize)>, SummersetError> {
        let (mut key, mut ver) = (None, 0);
        for inst in self.insts.iter_mut() {
            if inst.status >= Status::Committed {
                for (_, req) in &inst.reqs {
                    if let ApiRequest::Req {
                        cmd: Command::Put { key: k, .. },
                        ..
                    } = req
                    {
                        if key.is_none() {
                            key = Some(k.into());
                        } else if key.as_ref().unwrap() == k {
                            ver += 1;
                        }
                    }
                }
            }
        }

        Ok(key.map(|k| (k, ver)))
    }
}
