import sys
import os
import time

sys.path.append(os.path.dirname(os.path.realpath(__file__)))
from proc import run_process, run_process_over_ssh, wait_parallel_procs


def lookup_dns_to_ip(domain):
    proc = run_process(["dig", "+short", domain], capture_stdout=True, print_cmd=False)
    out, _ = proc.communicate()
    out = out.decode().strip()
    if len(out) == 0:
        raise RuntimeError(f"dns lookup for {domain} failed")

    ip = out.split("\n")[0]
    assert ip.count(".") == 3
    return ip


def get_interface_name(remote=None):
    cmd = ["ip", "-o", "-4", "route", "show", "to", "default"]
    proc = None
    if remote is None:
        proc = run_process(cmd, capture_stdout=True, print_cmd=False)
    else:
        proc = run_process_over_ssh(remote, cmd, capture_stdout=True, print_cmd=False)
    out, _ = proc.communicate()
    out = out.decode().strip()

    segs = out.split()
    assert len(segs) >= 5
    return segs[4]


def set_tc_qdisc_netem(
    netns, dev, mean, jitter, rate, distribution="pareto", remote=None
):
    QLEN_LIMIT = 500000000
    delay_args = f"delay {mean}ms" if mean > 0 else ""
    jitter_args = (
        f"{jitter}ms distribution {distribution}" if mean > 0 and jitter > 0 else ""
    )
    rate_args = f"rate {rate}gibit" if rate > 0 else ""
    cmd = [
        "tc",
        "qdisc",
        "replace",
        "dev",
        dev,
        "root",
        "netem",
        delay_args,
        jitter_args,
        rate_args,
    ]
    if netns is not None and len(netns) > 0:
        cmd = ["sudo", "ip", "netns", "exec", netns] + cmd + ["limit", str(QLEN_LIMIT)]
    else:
        cmd = ["sudo"] + cmd

    if remote is None:
        return run_process(cmd)
    else:
        return run_process_over_ssh(
            remote,
            cmd,
            print_cmd=False,
        )


def set_tc_qdisc_netems_veth(
    num_replicas,
    netns,
    dev,
    ifb,
    mean,
    jitter,
    rate,
    distribution="pareto",
    involve_ifb=False,
    remote=None,
):
    for replica in range(num_replicas):
        set_tc_qdisc_netem(
            netns(replica),
            dev(replica),
            mean(replica),
            jitter(replica),
            rate(replica),
            distribution=distribution,
            remote=remote,
        ).wait()
        set_tc_qdisc_netem(
            netns(replica),
            ifb(replica),
            0,
            0,
            rate(replica) if involve_ifb else 0,
            remote=remote,
        ).wait()


def set_tc_qdisc_netems_main(
    mean, jitter, rate, distribution="pareto", involve_ifb=False, remotes=None
):
    if remotes is None:
        remotes = [None]
    else:
        remotes = [remotes[h] for h in sorted(list(remotes.keys()))]

    procs = []
    for replica, remote in enumerate(remotes):
        procs.append(
            set_tc_qdisc_netem(
                None,
                get_interface_name(remote=remote),
                mean(replica),
                jitter(replica),
                rate(replica),
                distribution=distribution,
                remote=remote,
            )
        )
    wait_parallel_procs(procs, check_rc=False)

    procs = []
    for replica, remote in enumerate(remotes):
        procs.append(
            set_tc_qdisc_netem(
                None,
                "ifbe",
                0,
                0,
                rate(replica) if involve_ifb else 0,
                distribution=distribution,
                remote=remote,
            )
        )
    wait_parallel_procs(procs, check_rc=False)


def add_tc_qdisc_prio_base(dev, num_bands=4, remote=None):
    assert num_bands > 3

    cmd = [
        "tc",
        "qdisc",
        "replace",
        "dev",
        dev,
        "root",
        "handle",
        "1:",
        "prio",
        "bands",
        str(num_bands),
    ]
    cmd = ["sudo"] + cmd

    if remote is None:
        return run_process(cmd)
    else:
        return run_process_over_ssh(
            remote,
            cmd,
            print_cmd=False,
        )


def add_tc_qdisc_band_netem(
    dev,
    mean,
    jitter,
    rate,
    distribution="pareto",
    flowid="1:4",
    remote=None,
):
    assert flowid.count(":") == 1 and flowid[-1] != ":"
    child = flowid.split(":")[-1] + "0:"

    delay_args = f"delay {mean}ms" if mean > 0 else ""
    jitter_args = (
        f"{jitter}ms distribution {distribution}" if mean > 0 and jitter > 0 else ""
    )
    rate_args = f"rate {rate}gibit" if rate > 0 else ""
    cmd = [
        "tc",
        "qdisc",
        "add",
        "dev",
        dev,
        "parent",
        flowid,
        "handle",
        child,
        "netem",
        delay_args,
        jitter_args,
        rate_args,
    ]
    cmd = ["sudo"] + cmd

    if remote is None:
        return run_process(cmd)
    else:
        return run_process_over_ssh(
            remote,
            cmd,
            print_cmd=False,
        )


def add_tc_filter_for_ip_dst(
    dev, ip_dst, higher_ports=False, flowid="1:4", remote=None
):
    assert flowid.count(":") == 1 and flowid[-1] != ":"

    cmd = [
        "tc",
        "filter",
        "add",
        "dev",
        dev,
        "protocol",
        "ip",
        "parent",
        "1:0",
        "prio",
        str(1),
        "u32",
        "match",
        "ip",
        "dst",
        f"{ip_dst}/32",
    ]
    # if higher_ports:
    #     # combination of dport 32768 with mask 0x8000 is equivalent to
    #     # selecting ports >= 32768
    #     cmd += [
    #         "match",
    #         "ip",
    #         "dport",
    #         str(32768),
    #         "0x8000",
    #     ]
    cmd += [
        "flowid",
        flowid,
    ]
    cmd = ["sudo"] + cmd

    if remote is None:
        return run_process(cmd)
    else:
        return run_process_over_ssh(
            remote,
            cmd,
            print_cmd=False,
        )


def set_tc_qdisc_netems_asym(
    pairs_mean,
    pairs_jitter,
    pairs_rate,
    distribution="pareto",
    higher_ports=False,
    remotes=None,
    ipaddrs=None,
):
    """
    Similar to set_tc_qdisc_netems_main but sets each pair of servers separately
    through 'tc filter' rules, giving a real, asymmetric RTTs topology.
    """
    assert remotes is not None and len(remotes) > 1
    assert len(ipaddrs) == len(remotes)
    host_idx = {h: i for i, h in enumerate(sorted(list(remotes.keys())))}
    main_dev = {h: get_interface_name(remote=remote) for h, remote in remotes.items()}

    procs = []
    for host, remote in remotes.items():
        # add base prio qdisc
        procs.append(
            add_tc_qdisc_prio_base(
                main_dev[host],
                num_bands=3 + len(remotes),
                remote=remote,
            )
        )
    wait_parallel_procs(procs, check_rc=False)

    procs = []
    for host, remote in remotes.items():
        # add per-peer child netem qdiscs
        for peer in remotes.keys():
            procs.append(
                add_tc_qdisc_band_netem(
                    main_dev[host],
                    pairs_mean.get(host, peer),
                    pairs_jitter.get(host, peer),
                    pairs_rate.get(host, peer),
                    distribution=distribution,
                    flowid=f"1:{3 + host_idx[peer] + 1}",
                    remote=remote,
                )
            )
    wait_parallel_procs(procs, check_rc=False)

    procs = []
    for host, remote in remotes.items():
        # add filter rules for each peer
        for peer, peer_ip in ipaddrs.items():
            procs.append(
                add_tc_filter_for_ip_dst(
                    main_dev[host],
                    peer_ip,
                    higher_ports=higher_ports,
                    flowid=f"1:{3 + host_idx[peer] + 1}",
                    remote=remote,
                )
            )
    wait_parallel_procs(procs, check_rc=False)


def clear_tc_qdisc_netem(netns, dev, remote=None, capture_stderr=False):
    cmd = [
        "tc",
        "qdisc",
        "delete",
        "dev",
        dev,
        "root",
    ]
    if netns is not None and len(netns) > 0:
        cmd = ["sudo", "ip", "netns", "exec", netns] + cmd
    elif dev == "ifbe":
        cmd = [
            "sudo",
            "tc",
            "qdisc",
            "replace",
            "dev",
            dev,
            "root",
            "noqueue",
        ]
    else:
        cmd = ["sudo"] + cmd

    if remote is None:
        return run_process(cmd, capture_stderr=capture_stderr)
    else:
        return run_process_over_ssh(
            remote,
            cmd,
            print_cmd=False,
            capture_stderr=capture_stderr,
        )


def clear_tc_qdisc_netems_veth(
    num_replicas, netns, dev, ifb, remote=None, capture_stderr=False
):
    for replica in range(num_replicas):
        clear_tc_qdisc_netem(
            netns(replica),
            dev(replica),
            remote=remote,
            capture_stderr=capture_stderr,
        ).wait()
        clear_tc_qdisc_netem(
            netns(replica),
            ifb(replica),
            remote=remote,
            capture_stderr=capture_stderr,
        ).wait()


def clear_tc_qdisc_netems_main(remotes=None, capture_stderr=False):
    if remotes is None:
        remotes = [None]
    else:
        remotes = [remotes[h] for h in sorted(list(remotes.keys()))]

    procs = []
    for remote in remotes:
        procs.append(
            clear_tc_qdisc_netem(
                None,
                get_interface_name(remote=remote),
                remote=remote,
                capture_stderr=capture_stderr,
            )
        )
    wait_parallel_procs(procs, check_rc=False)

    procs = []
    for remote in remotes:
        procs.append(
            clear_tc_qdisc_netem(
                None,
                "ifbe",
                remote=remote,
                capture_stderr=capture_stderr,
            )
        )
    wait_parallel_procs(procs, check_rc=False)
