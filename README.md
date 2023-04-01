# tmpi.py

This is a python rewrite of [tmpi](https://github.com/Azrael3000/tmpi) with the following benefits:

* Faster startup
* Support mpirun with multiple hosts (with `MPIRUNARGS="--host ..." ./tmpi.py ...`)
* mpi rank and host are shown in the upper frame of each pane

It has the following disadvantages:

* you can run only one instance of `tmpi.py` on a single host
* if you have mpi processes on other hosts, you need to be able to ssh into them
* the tmux panes are not created in the order of the mpi ranks
* `tmpi.py` opens a port for remote mpi processes to register themselves. This can be a security issue.

Run multiple MPI processes as a grid in a new tmux window and multiplex keyboard input to all of them.

## Dependencies
- [tmux](https://github.com/tmux/tmux/wiki)
- [Reptyr](https://github.com/nelhage/reptyr) 
- Python 3.7 or later

## Further requirements
- If you run mpi processes on remote hosts, you need to be able to ssh into them with the user that started `tmpi.py` without any password prompt

## Installation
Just copy the `tmpi.py` script somewhere in your `PATH`.

## Example usage

Parallel debugging with GDB:
```
tmpi.py 4 gdb executable
```

It is advisable to run gdb with a script (e.g. `script.gdb`) so you can use
```
tmpi.py 4 gdb -x script.gdb executable
```

If you have a lot of processors you want to have `set pagination off` and add the `-q` argument to gdb:
```
tmpi.py 4 gdb -q -x script.gdb executable
```
This avoids pagination and the output of the copyright of gdb, which can be a nuissance when you have very small tmux panes.

## Full usage
See `def usage()` in the [script](tmpi)
