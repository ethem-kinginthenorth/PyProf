#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright (c) 2020, NVIDIA CORPORATION. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
This file intercepts (monkey patches) the following functions and adds NVTX markers.
	torch.*
	torch.Tensor.*
	torch.nn.functional.*
	torch.nn.*.forward

The NVTX markers (one or more) contain the following information
	call trace (a list of file_name:line_number)
	extra_repr() from torch.nn modules
	module/class name
	function name
	inputs (args and kwargs)
		scalar: name, type and value
		tensor: name, shape and datatype
		numpy: name, shape and datatype
		list/tuple: a sequence of scalars or tensors or numpy arrays
"""

import torch
import torch.cuda.nvtx as nvtx
import numpy
import inspect as ins
import traceback
import math
import json

def isfunc(mod, f):
    assert hasattr(mod, f)
    attr = getattr(mod, f)

    #Ignore functions like _add
    if (len(f) >= 2):
        if f[0] == "_" and f[1] != "_":
            return False

    #Ignore functions from this list
    ignore = [
        '__all__', '__array__', '__array_priority__', '__array_wrap__', '__bool__', '__builtins__', '__cached__',
        '__class__', '__deepcopy__', '__delattr__', '__delitem__', '__dict__', '__dir__', '__doc__', '__file__',
        '__format__', '__getattribute__', '__getitem__', '__hash__', '__index__', '__init__', '__init_subclass__',
        '__iter__', '__len__', '__loader__', '__module__', '__name__', '__new__', '__nonzero__', '__package__',
        '__path__', '__reduce__', '__reduce_ex__', '__repr__', '__reversed__', '__setattr__', '__setitem__',
        '__setstate__', '__sizeof__', '__spec__', '__str__', '__subclasshook__', '__version__', '__weakref__'
    ]

    #Add functions to this list if they cause recursion
    ignore += ['size', 'tolist', 'dim', 'is_storage', 'item']
    if f in ignore:
        return False

    return ins.ismethod(attr) or ins.isfunction(attr) or ins.ismethoddescriptor(attr) or ins.isbuiltin(attr)


# Returns a dict string with a tracemarker and function stack in it
#
def traceMarker():
    # Returns a string representing the stack of function calls separated with '/'
    #
    def get_func_stack():
        func_stack = ""
        ins_stack = ins.stack()

        # Starting at index of 3 to ignore this function, it's parent (traceMarker) and it's parent (wrapper_func)
        #
        for i in range(3, len(ins_stack)):
            frame = ins_stack[i]
            fn_name = frame[0].f_code.co_name
            frame_info = ""

            # __call__:  Much of Torch library is implemented in this way. Ignore these extra layers
            # wrapper_func: Is a function in this file. If there are nested monkeypatched functions we don't want it to show up
            # <module>: Just the top level module. Doesn't add any information and if it exists in any html it breaks it
            #
            if (fn_name in ["__call__","wrapper_func","<module>"]):
                continue

            # Grab class name if it exists
            #
            if 'self' in frame[0].f_locals:
                cls_name = frame[0].f_locals['self'].__class__.__name__
                frame_info += cls_name + "::"

            frame_info += fn_name

            # Prepend this frame's info into the function stack
            #
            func_stack = '/' + frame_info + func_stack

        return func_stack

    # Return a trace marker string
    #
    def get_trace_marker():
        cadena = []
        stack = traceback.extract_stack()
        
        # Starting at index of 3 to ignore this function, it's parent (traceMarker) and it's parent (wrapper_func)
        #
        for i in range(len(stack) - 3):
            fi = stack[i]
            t = "{}:{}".format(fi.filename, fi.lineno)
            cadena.append(t)
        return cadena

    d = {}
    d['traceMarker'] = get_trace_marker()
    #d['funcStack'] = get_func_stack()
    return str(d)


def modMarker(mod, fn_name, args):
    """
	Returns the stringified extra_repr() of a module.
	"""
    assert (fn_name == 'forward')
    assert (len(args) > 0)
    d = {}
    d['mod'] = mod.__name__
    d['strRepr'] = args[0].extra_repr()
    return str(d)


def add_wrapper(mod, fn_name):
    assert isfunc(mod, fn_name)

    # Get a pointer to the original function
    func = getattr(mod, fn_name)

    # Check if the mod has a string representation
    # and is not a Script or Traced module (used by JIT)
    s = hasattr(mod, "extra_repr") and (type(mod) is not torch.jit.ScriptModule
                                       ) and (type(mod) is not torch.jit.TopLevelTracedModule)

    def wrapper_func(*args, **kwargs):

        # Push trace marker
        nvtx.range_push(traceMarker())

        # Push module marker
        if s:
            m = modMarker(mod, fn_name, args)
            nvtx.range_push(m)

        # Create and push argument marker
        cadena = argMarker(mod, fn_name, args, kwargs)
        nvtx.range_push(cadena)

        # Call the original function
        result = func(*args, **kwargs)

        # Pop argumet marker
        nvtx.range_pop()

        # Pop module marker
        if s:
            nvtx.range_pop()

        # Pop trace marker
        nvtx.range_pop()

        return result

    setattr(mod, fn_name, wrapper_func)


def argMarker(mod, op, args, kwargs):
    #For this function args is a tuple and kwargs is a dict

    def tensor(arg, name=""):
        a = {}
        a['name'] = name
        a['type'] = "tensor"
        a['shape'] = tuple(arg.size())
        a['dtype'] = str(arg.dtype).split(".")[-1]
        cadena['args'].append(a)

    def ndarray(arg, name=""):
        a = {}
        a['name'] = name
        a['type'] = "ndarray"
        a['shape'] = arg.shape
        a['dtype'] = str(arg.dtype).split(".")[-1]
        cadena['args'].append(a)

    def seq(arg, name=""):
        assert issequence(arg)
        a = {}
        a['name'] = name
        if isinstance(arg, list):
            a['type'] = "list"
            a['value'] = arg
        else:
            a['type'] = "tuple"
            # The arg could be torch.Size, which is a subclass of tuple
            # Therefore, explicitly convert to tuple
            a['value'] = tuple(arg)

        cadena['args'].append(a)

    def scalar(arg, name=""):
        a = {}
        a['name'] = name
        a['type'] = type(arg).__name__
        #handle the case when the argument is +/- inf or nan
        if arg == float('inf'):
            a['value'] = "inf"
        elif arg == float('-inf'):
            a['value'] = "-inf"
        elif isinstance(arg, float) and math.isnan(arg):
            a['value'] = "nan"
        else:
            a['value'] = arg
        cadena['args'].append(a)

    def isscalar(arg):
        return (type(arg) is int) or (type(arg) is float) or (type(arg) is bool) or (arg is None) or (type(arg) is str)

    def issequence(arg):
        return isinstance(arg, list) or isinstance(arg, tuple)

    def foo(args, name):
        #args should be an iterable sequence e.g. list or tuple
        for arg in args:
            if isinstance(arg, torch.Tensor):
                if arg.dim() == 0:
                    scalar(arg.item(), name)
                else:
                    tensor(arg, name)
            elif isinstance(arg, numpy.ndarray):
                ndarray(arg, name)
            elif (isscalar(arg)):
                scalar(arg, name)
            elif issequence(arg):
                if (len(arg) == 0) or isscalar(arg[0]):  #An empty sequence or a sequence of scalars
                    seq(arg, name)
                else:  # A sequence of tensors or numpy arrays
                    foo(arg, name)
            '''
			else:
				print("The following arg is none of Tensor, numpy array, scalar but a %s" % (str(type(arg))))
				print("Mod: %s" % str(mod.__name__))
				print("Op: %s" % str(op))
				print(dir(arg))
			'''

    cadena = {}
    cadena['mod'] = mod.__name__
    cadena['op'] = op
    cadena['args'] = []

    foo(args, "")
    for k, v in kwargs.items():
        foo((v, ), k)

    return str(cadena)


def patchClass(cls):
    for f in dir(cls):
        if isfunc(cls, f):
            add_wrapper(cls, f)

def patch_torch_classes():
    """Monkey-patch all classes in torch"""
    for cls in [
            torch,
            torch.Tensor,
            torch.nn.functional,
    ]:
        patchClass(cls)


def patch_torch_nn_forward_functions():
    """Monkey-patch all forward functions in torch.nn libraries"""
    for cls in [torch.nn.RNN, torch.nn.RNNCell, torch.nn.LSTM, torch.nn.LSTMCell, torch.nn.GRU, torch.nn.GRUCell]:
        if isfunc(cls, 'forward'):
            add_wrapper(cls, 'forward')


def patch_dataloader():
    """Monkey-patch the dataloader in torch.utils.data"""
    mod = torch.utils.data.dataloader
    old_iter = mod.DataLoader.__iter__

    def new_iter(self, *args, **kwargs):

        # Push trace marker
        nvtx.range_push(traceMarker())

        # First pass is for creating the dataloader + returning the first data
        cadena = argMarker(mod, "DataLoader", args, kwargs)
        nvtx.range_push(cadena)

        for x in old_iter(self, *args, **kwargs):

            # Pop tracemarker
            nvtx.range_pop();           

            # Dataloader stop, Model start
            nvtx.range_pop() 

            yield x

            # Push trace marker
            nvtx.range_push(traceMarker())

            # Model stop, dataloader start
            cadena = argMarker(mod, "DataLoader", args, kwargs)
            nvtx.range_push(cadena)

        # Pop the last iteration before returning
        nvtx.range_pop()
        nvtx.range_pop()

    mod.DataLoader.__iter__ = new_iter

def patch_apex():
    """Monkey-patch functions in APEX"""
    import importlib
    if importlib.util.find_spec("amp_C") is not None:
        import amp_C
        patchClass(amp_C)

    if importlib.util.find_spec("fused_adam_cuda") is not None:
        import fused_adam_cuda
        patchClass(fused_adam_cuda)

    if importlib.util.find_spec("fused_layer_norm_cuda") is not None:
        import fused_layer_norm_cuda
        patchClass(fused_layer_norm_cuda)


def push_nvtx_model_config(config):
    """
    Helper function to dump the passed in dict config as an nvtx
    marker with "model_config" key
    """
    nvtx_msg = json.dumps({"model_config": config})
    nvtx.range_push(nvtx_msg)


def patch_dataloader_init():
    """
    Capture dataloader config (num_workers and pin_memory) and
    emit a model_config nvtx range with the information
    """
    mod = torch.utils.data.dataloader
    old_init = mod.DataLoader.__init__

    def new_init(self, *args, **kwargs):

        num_workers = kwargs.get("num_workers",0)
        pin_memory = kwargs.get("pin_memory", False)

        push_nvtx_model_config({"num_workers": num_workers, "pin_memory": pin_memory})
        old_init(self, *args, **kwargs)
        nvtx.range_pop()

    mod.DataLoader.__init__ = new_init


# Flag to indicate that cudnn_benchmark_disabled has already been reported
#
cudnn_benchmark_disabled_reported = False

def patch_with_always_benchmark(mod, fn_name):
    """
    Patch the given mod/function so that if it is ever executed and 
    torch.backends.cudnn.benchmark is not true, it will emit an nvtx
    range to report that fact
    """
    assert isfunc(mod, fn_name)
    old_fn = getattr(mod, fn_name)

    def always_benchmark_wrapper(*args, **kwargs):
        global cudnn_benchmark_disabled_reported

        add_nvtx = not torch.backends.cudnn.benchmark and not cudnn_benchmark_disabled_reported
        if add_nvtx:
            cudnn_benchmark_disabled_reported = True
            push_nvtx_model_config({"cudnn_benchmark_disabled": True})

        result = old_fn(*args, **kwargs)

        if add_nvtx:
            nvtx.range_pop()

        return result

    setattr(mod, fn_name, always_benchmark_wrapper)


def patch_never_call(mod, fn_name, key):
    """
    Patch the given mod/function. If the function is executed, emit 
    an nvtx_range with data indicating that 'key' was true
    """
    old_fn = getattr(mod, fn_name)

    def wrapper_func(*args, **kwargs):
        push_nvtx_model_config({key: True})
        result = old_fn(*args, **kwargs)
        nvtx.range_pop()
        return result

    setattr(mod, fn_name, wrapper_func)


def patch_never_call_with_args(mod, fn_name, key, bad_args):
    """
    Patch the given mod/function. If the function is executed 
    and any of the bad args have any of the listed bad values, 
    emit an nvtx_range with data indicating that 'key' was true
    """
    old_fn = getattr(mod, fn_name)

    def wrapper_func(*args, **kwargs):

        signature = ins.signature(old_fn)
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()

        problem = False
        for k,v in bound.arguments.items():
            if k in bad_args:
                if v in bad_args[k]:
                    problem = True

        if problem:
            push_nvtx_model_config({key: True})

        result = old_fn(*args, **kwargs)

        if problem:
            nvtx.range_pop()

        return result

    setattr(mod, fn_name, wrapper_func)


def patch_model_configs():
    """
    Patch functions that help gather high-level configuration options for the model.
    All resulting nvtx ranges will have 'model_config' as the primary key
    """

    patch_dataloader_init()

    patch_with_always_benchmark(torch.nn.functional, "conv1d")
    patch_with_always_benchmark(torch.nn.functional, "conv2d")
    patch_with_always_benchmark(torch.nn.functional, "conv3d")
    patch_with_always_benchmark(torch.nn.functional, "conv_transpose1d")
    patch_with_always_benchmark(torch.nn.functional, "conv_transpose2d")
    patch_with_always_benchmark(torch.nn.functional, "conv_transpose3d")

    patch_never_call(torch.autograd.detect_anomaly, "__init__", "detect_anomaly")
    patch_never_call(torch.autograd, "gradcheck", "gradcheck")
    patch_never_call(torch.autograd, "gradgradcheck", "gradgradcheck")
    patch_never_call(torch.autograd.profiler.record_function, "__init__", "record_function")

    patch_never_call_with_args(torch.autograd.profiler.profile, "__init__", "profile", {"enabled": {True}})
    patch_never_call_with_args(torch.autograd.set_detect_anomaly, "__init__", "detect_anomaly", {"mode": {True}})
    patch_never_call_with_args(torch.autograd.profiler.emit_nvtx, "__init__", "emit_nvtx", {"enabled": {True}})


def init():
    print("Initializing NVTX monkey patches")

    patch_dataloader()
    patch_torch_classes()
    patch_torch_nn_forward_functions()
    patch_apex()
    patch_model_configs()

    print("Done with NVTX monkey patching")
