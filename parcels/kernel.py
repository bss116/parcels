from parcels.codegenerator import KernelGenerator, LoopGenerator
from parcels.compiler import get_cache_dir
from parcels.kernels.error import ErrorCode, recovery_map as recovery_base_map
from parcels.field import FieldSamplingError
from os import path
import numpy.ctypeslib as npct
from ctypes import c_int, c_float, c_double, c_void_p, byref
from ast import parse, FunctionDef, Module
import inspect
from copy import deepcopy
import re
from hashlib import md5
import math  # noqa
import random  # noqa


__all__ = ['Kernel']


re_indent = re.compile(r"^(\s+)")


def fix_indentation(string):
    """Fix indentation to allow in-lined kernel definitions"""
    lines = string.split('\n')
    indent = re_indent.match(lines[0])
    if indent:
        lines = [l.replace(indent.groups()[0], '', 1) for l in lines]
    return "\n".join(lines)


class Kernel(object):
    """Kernel object that encapsulates auto-generated code.

    :arg grid: Grid object providing the field information
    :arg ptype: PType object for the kernel particle

    Note: A Kernel is either created from a compiled <function ...> object
    or the necessary information (funcname, funccode, funcvars) is provided.
    The py_ast argument may be derived from the code string, but for
    concatenation, the merged AST plus the new header definition is required.
    """

    def __init__(self, grid, ptype, pyfunc=None, funcname=None,
                 funccode=None, py_ast=None, funcvars=None):
        self.grid = grid
        self.ptype = ptype

        # Derive meta information from pyfunc, if not given
        self.funcname = funcname or pyfunc.__name__
        self.funcvars = funcvars or list(pyfunc.__code__.co_varnames)
        self.funccode = funccode or inspect.getsource(pyfunc.__code__)
        # Parse AST if it is not provided explicitly
        self.py_ast = py_ast or parse(fix_indentation(self.funccode)).body[0]
        if pyfunc is None:
            # Extract user context by inspecting the call stack
            stack = inspect.stack()
            try:
                user_ctx = stack[-1][0].f_globals
                user_ctx['math'] = globals()['math']
                user_ctx['random'] = globals()['random']
                user_ctx['ErrorCode'] = globals()['ErrorCode']
            except:
                print("Warning: Could not access user context when merging kernels")
                user_ctx = globals()
            finally:
                del stack  # Remove cyclic references
            # Compile and generate Python function from AST
            py_mod = Module(body=[self.py_ast])
            exec(compile(py_mod, "<ast>", "exec"), user_ctx)
            self.pyfunc = user_ctx[self.funcname]
        else:
            self.pyfunc = pyfunc
        self.name = "%s%s" % (ptype.name, self.funcname)

        # Generate the kernel function and add the outer loop
        if self.ptype.uses_jit:
            kernelgen = KernelGenerator(grid, ptype)
            self.field_args = kernelgen.field_args
            kernel_ccode = kernelgen.generate(deepcopy(self.py_ast),
                                              self.funcvars)
            self.field_args = kernelgen.field_args
            self.const_args = kernelgen.const_args
            loopgen = LoopGenerator(grid, ptype)
            self.ccode = loopgen.generate(self.funcname, self.field_args, self.const_args,
                                          kernel_ccode)

            basename = path.join(get_cache_dir(), self._cache_key)
            self.src_file = "%s.c" % basename
            self.lib_file = "%s.so" % basename
            self.log_file = "%s.log" % basename
        self._lib = None

    @property
    def _cache_key(self):
        field_keys = "-".join(["%s:%s" % (name, field.units.__class__.__name__)
                               for name, field in self.field_args.items()])
        key = self.name + self.ptype._cache_key + field_keys
        return md5(key.encode('utf-8')).hexdigest()

    def compile(self, compiler):
        """ Writes kernel code to file and compiles it."""
        with open(self.src_file, 'w') as f:
            f.write(self.ccode)
        compiler.compile(self.src_file, self.lib_file, self.log_file)
        print("Compiled %s ==> %s" % (self.name, self.lib_file))

    def load_lib(self):
        self._lib = npct.load_library(self.lib_file, '.')
        self._function = self._lib.particle_loop

    def execute_jit(self, pset, endtime, dt):
        """Invokes JIT engine to perform the core update loop"""
        fargs = [byref(f.ctypes_struct) for f in self.field_args.values()]
        fargs += [c_float(f) for f in self.const_args.values()]
        particle_data = pset._particle_data.ctypes.data_as(c_void_p)
        self._function(c_int(len(pset)), particle_data,
                       c_double(endtime), c_float(dt), *fargs)

    def execute_python(self, pset, endtime, dt):
        """Performs the core update loop via Python"""
        sign = 1. if dt > 0. else -1.
        for p in pset.particles:
            # Compute min/max dt for first timestep
            dt_pos = min(abs(p.dt), abs(endtime - p.time))
            while dt_pos > 0:
                try:
                    res = self.pyfunc(p, pset.grid, p.time, sign * dt_pos)
                except FieldSamplingError as fse:
                    res = ErrorCode.ErrorOutOfBounds
                    p.exception = fse
                except Exception as e:
                    res = ErrorCode.Error
                    p.exception = e

                # Update particle state for explicit returns
                if res is not None:
                    p.state = res

                # Handle particle time and time loop
                if res is None or res == ErrorCode.Success:
                    # Update time and repeat
                    p.time += sign * dt_pos
                    dt_pos = min(abs(p.dt), abs(endtime - p.time))
                    continue
                elif res == ErrorCode.Repeat:
                    # Try again without time update
                    dt_pos = min(abs(p.dt), abs(endtime - p.time))
                    continue
                else:
                    break  # Failure - stop time loop

    def execute(self, pset, endtime, dt, recovery=None):
        """Execute this Kernel over a ParticleSet for several timesteps"""

        def remove_deleted(pset):
            """Utility to remove all particles that signalled deletion"""
            indices = [i for i, p in enumerate(pset.particles)
                       if p.state in [ErrorCode.Delete]]
            pset.remove(indices)

        if recovery is None:
            recovery = {}
        recovery_map = recovery_base_map.copy()
        recovery_map.update(recovery)

        # Execute the kernel over the particle set
        if self.ptype.uses_jit:
            self.execute_jit(pset, endtime, dt)
        else:
            self.execute_python(pset, endtime, dt)

        # Remove all particles that signalled deletion
        remove_deleted(pset)

        # Idenitify particles that threw errors
        error_particles = [p for p in pset.particles
                           if p.state not in [ErrorCode.Success, ErrorCode.Repeat]]
        while len(error_particles) > 0:
            # Apply recovery kernel
            for p in error_particles:
                recovery_kernel = recovery_map[p.state]
                p.state = ErrorCode.Success
                recovery_kernel(p)

            # Remove all particles that signalled deletion
            remove_deleted(pset)

            # Execute core loop again to continue interrupted particles
            if self.ptype.uses_jit:
                self.execute_jit(pset, endtime, dt)
            else:
                self.execute_python(pset, endtime, dt)

            error_particles = [p for p in pset.particles
                               if p.state not in [ErrorCode.Success, ErrorCode.Repeat]]

    def merge(self, kernel):
        funcname = self.funcname + kernel.funcname
        func_ast = FunctionDef(name=funcname, args=self.py_ast.args,
                               body=self.py_ast.body + kernel.py_ast.body,
                               decorator_list=[], lineno=1, col_offset=0)
        return Kernel(self.grid, self.ptype, pyfunc=None,
                      funcname=funcname, funccode=self.funccode + kernel.funccode,
                      py_ast=func_ast, funcvars=self.funcvars + kernel.funcvars)

    def __add__(self, kernel):
        if not isinstance(kernel, Kernel):
            kernel = Kernel(self.grid, self.ptype, pyfunc=kernel)
        return self.merge(kernel)

    def __radd__(self, kernel):
        if not isinstance(kernel, Kernel):
            kernel = Kernel(self.grid, self.ptype, pyfunc=kernel)
        return kernel.merge(self)
