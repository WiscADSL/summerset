//! Benchmarking client using open-loop driver.

use crate::drivers::DriverOpenLoop;

use lazy_static::lazy_static;

use rand::Rng;
use rand::distributions::Alphanumeric;
use rand::rngs::ThreadRng;

use serde::Deserialize;

use tokio::time::{self, Duration, Instant, Interval, MissedTickBehavior};

use summerset::{
    GenericEndpoint, RequestId, SummersetError, pf_error, logged_err,
    parsed_config,
};

lazy_static! {
    /// Pool of keys to choose from.
    // TODO: enable using a dynamic pool of keys
    static ref KEYS_POOL: Vec<String> = {
        let mut pool = vec![];
        for _ in 0..5 {
            let key = rand::thread_rng()
                .sample_iter(&Alphanumeric)
                .take(8)
                .map(char::from)
                .collect();
            pool.push(key)
        }
        pool
    };

    /// Statistics printing interval.
    static ref PRINT_INTERVAL: Duration = Duration::from_millis(500);
}

/// Mode parameters struct.
#[derive(Debug, Deserialize)]
pub struct ModeParamsBench {
    /// Target frequency of issuing requests per second.
    pub freq_target: u64,

    /// Time length to benchmark in seconds.
    pub length_s: u64,

    /// Percentage of put requests.
    pub put_ratio: u8,

    /// Value size in bytes.
    pub value_size: usize,
}

#[allow(clippy::derivable_impls)]
impl Default for ModeParamsBench {
    fn default() -> Self {
        ModeParamsBench {
            freq_target: 200000,
            length_s: 30,
            put_ratio: 50,
            value_size: 1024,
        }
    }
}

/// Benchmarking client struct.
pub struct ClientBench {
    /// Open-loop request driver.
    driver: DriverOpenLoop,

    /// Mode parameters struct.
    params: ModeParamsBench,

    /// Random number generator.
    rng: ThreadRng,

    /// Fixed value generated according to specified size.
    value: String,
}

impl ClientBench {
    /// Creates a new benchmarking client.
    pub fn new(
        endpoint: Box<dyn GenericEndpoint>,
        timeout: Duration,
        params_str: Option<&str>,
    ) -> Result<Self, SummersetError> {
        let params = parsed_config!(params_str => ModeParamsBench;
                                     freq_target, length_s, put_ratio,
                                     value_size)?;
        if params.freq_target > 10000000 {
            return logged_err!("c"; "invalid params.freq_target '{}'",
                                   params.freq_target);
        }
        if params.length_s == 0 {
            return logged_err!("c"; "invalid params.length_s '{}'",
                                   params.length_s);
        }
        if params.put_ratio > 100 {
            return logged_err!("c"; "invalid params.put_ratio '{}'",
                                   params.put_ratio);
        }
        if params.value_size == 0 {
            return logged_err!("c"; "invalid params.value_size '{}'",
                                   params.value_size);
        }

        let value = rand::thread_rng()
            .sample_iter(&Alphanumeric)
            .take(params.value_size)
            .map(char::from)
            .collect();

        Ok(ClientBench {
            driver: DriverOpenLoop::new(endpoint, timeout),
            params,
            rng: rand::thread_rng(),
            value,
        })
    }

    /// Issues a random request.
    fn issue_rand_cmd(&mut self) -> Result<Option<RequestId>, SummersetError> {
        let key = KEYS_POOL[self.rng.gen_range(0..KEYS_POOL.len())].clone();
        if self.rng.gen_range(0..=100) <= self.params.put_ratio {
            self.driver.issue_put(&key, &self.value)
        } else {
            self.driver.issue_get(&key)
        }
    }

    /// Runs one iteration action of closed-loop style benchmark.
    #[allow(clippy::too_many_arguments)]
    async fn closed_loop_iter(
        &mut self,
        total_cnt: &mut u64,
        reply_cnt: &mut u64,
        chunk_cnt: &mut u64,
        chunk_lats: &mut Vec<f64>,
        retrying: &mut bool,
    ) -> Result<(), SummersetError> {
        // send next request
        let req_id = if *retrying {
            self.driver.issue_retry()?
        } else {
            self.issue_rand_cmd()?
        };

        *retrying = req_id.is_none();
        if !*retrying {
            *total_cnt += 1;
        }

        // wait for the next reply
        if *total_cnt > *reply_cnt {
            let result = self.driver.wait_reply().await?;

            if let Some((_, _, lat)) = result {
                *reply_cnt += 1;
                *chunk_cnt += 1;
                let lat_us = lat.as_secs_f64() * 1000000.0;
                chunk_lats.push(lat_us);
            }
        }

        Ok(())
    }

    /// Runs one iteration action of open-loop style benchmark.
    #[allow(clippy::too_many_arguments)]
    async fn open_loop_iter(
        &mut self,
        total_cnt: &mut u64,
        reply_cnt: &mut u64,
        chunk_cnt: &mut u64,
        chunk_lats: &mut Vec<f64>,
        retrying: &mut bool,
        slowdown: &mut bool,
        ticker: &mut Interval,
    ) -> Result<(), SummersetError> {
        tokio::select! {
            // prioritize receiving reply
            biased;

            // receive next reply
            result = self.driver.wait_reply() => {
                if let Some((_, _, lat)) = result? {
                    *reply_cnt += 1;
                    *chunk_cnt += 1;
                    let lat_us = lat.as_secs_f64() * 1000000.0;
                    chunk_lats.push(lat_us);

                    if *slowdown {
                        *slowdown = false;
                    }
                }
            }

            // send next request
            _ = ticker.tick(), if !*slowdown => {
                let req_id = if *retrying {
                    self.driver.issue_retry()?
                } else {
                    self.issue_rand_cmd()?
                };

                *retrying = req_id.is_none();
                *slowdown = *retrying && (*total_cnt > *reply_cnt);
                if !*retrying {
                    *total_cnt += 1;
                }
            }
        }

        Ok(())
    }

    /// Runs the adaptive benchmark for given time length.
    pub async fn run(&mut self) -> Result<(), SummersetError> {
        self.driver.connect().await?;
        println!(
            "{:^11} | {:^12} | {:^12} | {:>8} / {:<8}",
            "Elapsed (s)", "Tpt (reqs/s)", "Lat (us)", "Reply", "Total"
        );

        let mut freq_ticker = if self.params.freq_target > 0 {
            // open-loop, kick off frequency interval ticker
            // kick off frequency interval ticker
            let delay =
                Duration::from_nanos(1000000000 / self.params.freq_target);
            let mut ticker = time::interval(delay);
            ticker.set_missed_tick_behavior(MissedTickBehavior::Skip);
            Some(ticker)
        } else {
            None
        };

        let start = Instant::now();
        let (mut now, mut last_print) = (start, start);
        let length = Duration::from_secs(self.params.length_s);

        let (mut total_cnt, mut reply_cnt) = (0, 0);
        let mut chunk_cnt = 0;
        let mut chunk_lats: Vec<f64> = vec![];

        // run for specified length
        let (mut slowdown, mut retrying) = (false, false);
        while now.duration_since(start) < length {
            if self.params.freq_target == 0 {
                self.closed_loop_iter(
                    &mut total_cnt,
                    &mut reply_cnt,
                    &mut chunk_cnt,
                    &mut chunk_lats,
                    &mut retrying,
                )
                .await?;
            } else {
                self.open_loop_iter(
                    &mut total_cnt,
                    &mut reply_cnt,
                    &mut chunk_cnt,
                    &mut chunk_lats,
                    &mut retrying,
                    &mut slowdown,
                    freq_ticker.as_mut().unwrap(),
                )
                .await?;
            }

            now = Instant::now();

            // print statistics if print interval passed
            let elapsed = now.duration_since(start);
            let print_elapsed = now.duration_since(last_print);
            if print_elapsed >= *PRINT_INTERVAL {
                let tpt = (chunk_cnt as f64) / print_elapsed.as_secs_f64();
                let lat = if chunk_lats.is_empty() {
                    0.0
                } else {
                    chunk_lats.iter().sum::<f64>() / (chunk_lats.len() as f64)
                };
                println!(
                    "{:>11.2} | {:>12.2} | {:>12.2} | {:>8} / {:<8}",
                    elapsed.as_secs_f64(),
                    tpt,
                    lat,
                    reply_cnt,
                    total_cnt
                );
                last_print = now;
                chunk_cnt = 0;
                chunk_lats.clear();
            }
        }

        self.driver.leave(true).await?;
        Ok(())
    }
}