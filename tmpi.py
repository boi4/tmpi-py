#!/usr/bin/env python3
import subprocess

# Copyright 2023 Jan Fecht <fecht@mail.cc>
# Distributed under the terms of the GNU General Public License v2
#
# Based on Benedikt Morbach's tmpi bash script:
# https://github.com/Azrael3000/tmpi
# Copyright 2013 Benedikt Morbach <moben@exherbo.org>
# Distributed under the terms of the GNU General Public License v2
#
# runs multiple MPI processes on multiple hosts as a grid in a new
# tmux window and multiplexes keyboard input to all of them

import asyncio
import distutils.spawn
import fcntl
import json
import os
import random
import socket
import shlex
import sys
import termios
import tempfile
import time

# the address and port that the program will listen to new mpi processes
LISTEN_ADDRESS = "0.0.0.0"
LISTEN_PORT = 57571


# environment variables
MPIRUN_CMD=os.environ.get("MPIRUN", "mpirun")
MPIRUN_CMD_ARGS=os.environ.get("MPIRUNARGS", "")
REPTYR_CMD=os.environ.get("REPTYR", "reptyr")
TMPI_TMUX_OPTIONS=os.environ.get('TMPI_TMUX_OPTIONS', '')
TMPI_REMAIN=os.environ.get('TMPI_REMAIN', '')



async def run_cmd(cli, capture_stdout=False, exec=False, ignore_ret=False, background=False):
    if isinstance(cli, str):
        args = shlex.split(cli)
    else:
        args = cli

    if exec:
        os.execlp(args[0], *args)

    #proc = subprocess.Popen(args, stdout=subprocess.PIPE if capture_stdout else None)
    proc = await asyncio.create_subprocess_exec(*args, stdout=subprocess.PIPE if capture_stdout else None)
    if not background:
        stdout, _ = await proc.communicate()
        if proc.returncode != 0 and not ignore_ret:
            raise RuntimeError(f"command failed: {args!r}")
        if capture_stdout:
            return stdout
    else:
        return proc




async def handle_request(reader, writer, tmux_window, kill_dummy_event, tmux_lock):
    """
    Controller side, async, handling of a mpi process registration
    """
    # receive information about the mpi process
    data = await reader.read(255)

    message = data.decode()
    client_info = json.loads(message)

    #addr = writer.get_extra_info('peername')
    #print(f"Received {message!r} from {addr!r}")

    controller_hostname = socket.gethostname()
    mpi_host = client_info["client_hostname"]
    mpi_pid = client_info["client_tmpi_pid"]
    tmpfile = client_info["tmpfile"]
    mpi_rank = client_info["mpi_rank"]
    mpi_node_rank = client_info["mpi_node_rank"]

    # create new tmux pane
    async with tmux_lock:
        # run ssh with reptyr in tmux pane
        if mpi_host == controller_hostname:
            pane_cmd = f"bash -c "
        else:
            pane_cmd = f"ssh -t {mpi_host} "
        pane_cmd += f" 'cd {os.getcwd()}; {REPTYR_CMD} -l {sys.argv[0]} getreptyrpty {tmpfile}'"
        cmd = f"tmux split-window -d -P -F '#{{pane_id}} #{{pane_pid}}' {pane_cmd}"
        result = await run_cmd(cmd, capture_stdout=True)

        # kill dummy pane
        kill_dummy_event.set()

        pane,pane_pid = result.decode('utf-8').split()

        # add title to pane
        cmd = f'tmux select-pane -t {pane} -T "rank {mpi_rank} ({mpi_host})"'
        await run_cmd(cmd)

        # highlight world rank 0
        if mpi_rank == 0:
            cmd = f'tmux select-pane -t {pane} -T "#[fg=blue]rank {mpi_rank} ({mpi_host})"'
            await run_cmd(cmd)
            if TMPI_REMAIN in ["RANK0", "rank0"]:
                # keep rank 0
                cmd = f'tmux set-option -p -t {pane} remain-on-exit on'
                await run_cmd(cmd)

        #cmd = f"tmux select-layout -t {pane} even-horizontal" -> no grid, only columns
        cmd = f"tmux select-layout -t {pane} tiled"
        await run_cmd(cmd)

    answer = {}
    message = json.dumps(answer).encode('utf-8')
    writer.write(message)
    await writer.drain()

    # wait for last confirmation of read
    # > here we also now that the tmux ssh command was executed
    data = await reader.read(255)
    message = data.decode()

    # Close the connection
    writer.close()
    await writer.wait_closed()


async def kill_dummy_pane(event, dummy_pane, tmux_lock):
    await event.wait()
    #async with tmux_lock:
    print("Killing dummy pane")
    await run_cmd(f"tmux kill-pane -t '{dummy_pane}'")


async def run_server(tmux_window, dummy_pane, mpirun_proc, tmux_lock):
    """
    controller side server that waits for the mpi processes to register themselves
    """
    # event to communicate the creation of the first additional tmux pane
    kill_dummy_event = asyncio.Event()

    # setup waiting task to kill the dummy pane
    kill_dummy_task = asyncio.create_task(kill_dummy_pane(kill_dummy_event, dummy_pane, tmux_lock))

    # create actual server
    server = await asyncio.start_server(
        lambda r,w: handle_request(r,w, tmux_window, kill_dummy_event, tmux_lock),
        LISTEN_ADDRESS, LISTEN_PORT)

    # start serving
    addrs = ', '.join(str(sock.getsockname()) for sock in server.sockets)
    print(f'Serving on {addrs}')
    await server.start_serving()

    # wait for mpirun process to finish (ie mpi application is done)
    await mpirun_proc.communicate()
    server.close()
    await kill_dummy_task
    print("Finito")


async def controller(nprocs, cmd):
    """
    this function is called with the initial tmpi call
    it will start mpi and then manage the tmux window
    """
    # create a lock for tmux commands
    tmux_lock = asyncio.Lock()

    # rerun the program in tmux if not in tmux
    if not "TMUX" in os.environ:
        cmd = f"tmux {TMPI_TMUX_OPTIONS} -L tmpi.{random.randint(0,1000)} new-session " + " ".join(shlex.quote(a) for a in sys.argv)
        await run_cmd(cmd, exec=True) # no return

    async with tmux_lock:
        # create new window with dummy window
        ret = await run_cmd("tmux new-window -P -F '#{pane_id} #{window_id} #{session_id}'", capture_stdout=True)
        dummy, window, session = ret.decode('utf-8').split()

        # synchronize input to all panes.
        await run_cmd(f"tmux set-option -w -t {window} synchronize-panes on")
        # optionally let pane stay open after program exited
        if TMPI_REMAIN in ["1", "True", "true", "TRUE", "on", "yes"]:
            await run_cmd(f"tmux set-option -w -t {window} remain-on-exit on")
        else:
            await run_cmd(f"tmux set-option -w -t {window} remain-on-exit off")

        # show rank on top of each pane
        await run_cmd(f'tmux set-option -w -t {window} pane-border-format "#{{pane_title}}"')
        await run_cmd(f'tmux set-option -w -t {window} pane-border-status top')

    hostname = socket.gethostname()

    mpi_arg = f"{MPIRUN_CMD_ARGS} -x MPIRUNARGS"
    cmd = f"{MPIRUN_CMD} -n {nprocs} {mpi_arg} {sys.argv[0]} mpirun {hostname} {' '.join(shlex.quote(c) for c in cmd)}"

    # start mpi
    proc = await run_cmd(cmd, background=True)

    # start controller_server
    await run_server(window, dummy, proc, tmux_lock)




async def do_request(host, port, client_hostname, mpi_rank, mpi_node_rank):
    """
    register current mpi process at controller
    receive reptyr pty path
    """
    reader, writer = await asyncio.open_connection(
            host, port)

    t = tempfile.NamedTemporaryFile()

    # send message
    message = json.dumps({
        "client_hostname": client_hostname,
        "client_tmpi_pid": os.getpid(),
        "tmpfile": t.name,
        "mpi_rank": mpi_rank,
        "mpi_node_rank": mpi_node_rank,
    })
    writer.write(message.encode('utf-8'))
    await writer.drain()

    # receive (empty) answer
    data = await reader.read(255)
    message = data.decode('utf-8')
    answer = json.loads(message)

    while os.path.getsize(t.name) == 0:
        time.sleep(0.005)
    time.sleep(0.005)
    with open(t.name) as f:
        reptyr_pty = f.read().strip()

    # let other side know that we finished reading
    message = json.dumps({})
    writer.write(message.encode('utf-8'))
    await writer.drain()

    # close the connection
    writer.close()
    await writer.wait_closed()

    return reptyr_pty


async def mpiproc(args):
    """
    This is the main function of the mpi processes
    """
    controller_hostname = args[0]
    args = args[1:]

    hostname = socket.gethostname()
    mpi_rank = int(os.environ.get("OMPI_COMM_WORLD_RANK", os.environ.get("PMIX_RANK", "-1")))
    mpi_node_rank = int(os.environ.get("OMPI_COMM_WORLD_NODE_RANK", "-1"))

    # try to make rank 0 appear on top left pane
    if mpi_rank != 0:
        time.sleep(0.05)

    # Register ourselves at controller
    reptyr_pty = await do_request(controller_hostname, LISTEN_PORT, hostname, mpi_rank, mpi_node_rank)

    # run actual command using reptyr pty (which is connected to a tmux pane)
    await run_cmd(f"setsid {sys.argv[0]} reptyrattach {reptyr_pty} {' '.join(shlex.quote(arg) for arg in args)}")


def check_tools():
    tools = ["tmux", REPTYR_CMD, MPIRUN_CMD]

    for tool in tools:
        if not distutils.spawn.find_executable(tool):
            print(f"You need to install {tool}")
            sys.exit(-1)


def usage():
    print(f'''
tmpi.py: Run multiple MPI processes as a grid in a new tmux window and multiplex keyboard input to all of them.

Usage:
   {sys.argv[0]} [number] COMMAND ARG1 ...

You need to pass at least two arguments.
The first argument is the number of processes to use, every argument after that is the commandline to run.

If TMPI_REMAIN=true, ghe new window is set to remain on exit and has to be closed manually. ("C-b + &" by default)
You can pass additional 'mpirun' argument via the MPIRUNARGS environment variable
You can use the environment variable TMPI_TMUX_OPTIONS to pass options to the `tmux` invocation,
  such as '-f ~/.tmux.conf.tmpi' to use a special tmux configuration for tmpi.
Little usage hint: By default the panes in the window are synchronized. If you wish to work only with one thread maximize this pane ("C-b + z" by default) and work away on one thread. Return to all thread using the same shortcut.

Warning: This program listens for tcp connections at a port. Ideally you should only use it in a trusted network.
Also, because of that, currently only a single tmpi.py instance can be run at once.
'''.strip())


def main():
    """
    To remain a single script, the script implements multiple features.
    Features that are used internally, have a special argv[1]

    The initial process starts "mpirun tmpi.py mpirun ..." and then starts
    a tcp server to register the newly spawned mpi processes
    """
    if len(sys.argv) < 2:
        usage()

    if sys.argv[1] == "mpirun":
        # started via mpirun, "client"
        asyncio.run(mpiproc(sys.argv[2:]))
    elif sys.argv[1] == "getreptyrpty":
        # write environment variable to file
        print(socket.gethostname())
        reptyr_pty = os.getenv("REPTYR_PTY")
        with open(sys.argv[2], "w+") as f:
            f.write(reptyr_pty + "\n")
    elif sys.argv[1] == "reptyrattach":
        # make reptyr pty the main pty
        reptyr_pty = sys.argv[2]
        fd = os.open(reptyr_pty, os.O_RDWR)
        if fd == -1:
            os.perror("failed to attach to reptyr: open")
            sys.exit(1)

        if fcntl.ioctl(fd, termios.TIOCSCTTY, 0) != 0:
            os.perror("failed to attach to reptyr: ioctl")
            sys.exit(1)

        os.dup2(fd, 0)
        os.dup2(fd, 1)
        os.dup2(fd, 2)

        # exec the actual mpi program
        #print(" ".join(shlex.quote(s) for s in sys.argv[3:]))
        os.execvp(sys.argv[3], sys.argv[3:])
    else:
        # actual user invocation
        check_tools()
        asyncio.run(controller(int(sys.argv[1]), sys.argv[2:]))


if __name__ == "__main__":
    main()
