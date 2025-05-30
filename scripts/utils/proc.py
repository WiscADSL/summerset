import os
import subprocess
import multiprocessing


def kill_all_matching(name):
    print("Kill all:", name)
    assert name.count(" ") == 0
    cmd = f"sudo killall -9 {name} > /dev/null 2>&1"
    os.system(cmd)


def kill_all_local_procs():
    # print("Killing all local procs...")
    cmd = "./scripts/kill_all_procs.sh"
    os.system(cmd)


def kill_all_distr_procs(
    group, targets="all", chain=False, cockroach=False, etcd=False, zookeeper=False
):
    # print(f"Killing all procs on {group} {targets}...")
    cmd = [
        "python3",
        "scripts/remote_killall.py",
        "-g",
        group,
        "-t",
        targets,
    ]
    if chain:
        cmd.append("--chain")
    if cockroach:
        cmd.append("--cockroach")
    if etcd:
        cmd.append("--etcd")
    if zookeeper:
        cmd.append("--zookeeper")
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).wait()


def run_process(
    cmd,
    cd_dir=None,
    capture_stdout=False,
    capture_stderr=False,
    print_cmd=True,
    cpu_list=None,
    in_netns=None,
    extra_env=None,
    shell=False,
):
    stdout, stderr = None, None
    if capture_stdout:
        stdout = subprocess.PIPE
    if capture_stderr:
        stderr = subprocess.PIPE

    if cpu_list is not None and "-" in cpu_list:
        cmd = ["sudo", "taskset", "-c", cpu_list] + cmd

    if in_netns is not None and len(in_netns) > 0:
        cmd = [s for s in cmd if s != "sudo"]
        cmd = ["sudo", "ip", "netns", "exec", in_netns] + cmd

    env_vars = os.environ.copy()
    if extra_env is not None:
        env_vars.update(extra_env)

    if print_cmd:
        print("Run:", " ".join(cmd))

    proc = subprocess.Popen(
        cmd, cwd=cd_dir, stdout=stdout, stderr=stderr, env=env_vars, shell=shell
    )
    return proc


def run_process_over_ssh(
    remote,
    cmd,
    cd_dir=None,
    capture_stdout=False,
    capture_stderr=False,
    print_cmd=True,
    cpu_list=None,
    extra_env=None,
):
    stdout, stderr = None, None
    if capture_stdout:
        stdout = subprocess.PIPE
    if capture_stderr:
        stderr = subprocess.PIPE

    if cpu_list is not None and "-" in cpu_list:
        cmd = ["sudo", "taskset", "-c", cpu_list] + cmd

    if print_cmd:
        print(f"Run on {remote}: {' '.join(cmd)}")

    # ugly hack to solve the quote parsing issue
    config_seg = False
    for i, seg in enumerate(cmd):
        if seg.startswith("--"):
            if seg.strip() == "--config" or seg.strip() == "--params":
                config_seg = True
            else:
                config_seg = False
        elif config_seg:
            new_seg = "\\'".join(seg.split("'"))
            cmd[i] = new_seg
            config_seg = False

    str_cmd = " ".join(cmd)
    if extra_env is not None:
        extra_env_assigns = [f"{k}={v}" for k, v in extra_env.items()]
        str_cmd = f"{' '.join(extra_env_assigns)} {str_cmd}"

    if cd_dir is None or len(cd_dir) == 0:
        wrapped_cmd = f". ~/.profile; {str_cmd}"
    else:
        wrapped_cmd = f". ~/.profile; cd {cd_dir}; {str_cmd}"

    ssh_exec_cmd = ["ssh", "-o", "StrictHostKeyChecking=no", remote, wrapped_cmd]
    proc = subprocess.Popen(ssh_exec_cmd, stdout=stdout, stderr=stderr)
    return proc


def wait_parallel_procs(procs, names=None, check_rc=True):
    for i, proc in enumerate(procs):
        name = f"proc {i}" if names is None else names[i]
        rc = proc.wait()
        if check_rc:
            if rc == 0:
                print(f"  {name}: OK")
            else:
                print(f"  {name}: ERROR")
                if proc.stdout is not None:
                    print(proc.stdout.read().decode())
                if proc.stderr is not None:
                    print(proc.stderr.read().decode())


def get_cpu_count(remote=None):
    cpus = multiprocessing.cpu_count()
    if remote is not None:
        cpus = int(
            run_process_over_ssh(
                remote,
                ["sudo", "nproc"],
                capture_stdout=True,
                capture_stderr=True,
                print_cmd=False,
            )
            .communicate()[0]
            .decode()
            .strip()
        )
    return cpus


def check_enough_cpus(expected, remote=None):
    cpus = get_cpu_count(remote=remote)
    if cpus < expected:
        print(f"WARN: expect >= {expected} CPUs, found {cpus} on {remote}")
