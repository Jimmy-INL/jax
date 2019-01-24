# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from functools import partial

import numpy as onp
import six
from six.moves import reduce

from .. import core
from .. import linear_util as lu
from ..core import Trace, Tracer, Primitive, new_master
from ..abstract_arrays import ShapedArray, ConcreteArray, make_shaped_array
from ..util import safe_zip, unzip2, unzip3, partialmethod, prod
from ..lib import xla_bridge as xb
from . import partial_eval as pe
from . import pxla
from . import batching

zip = safe_zip

def identity(x): return x


### pmap


def pmap(fun, name, in_vals, in_axes, out_axis_target):
  sizes = reduce(set.union, map(batching.dimsize, in_axes, in_vals))
  if not sizes:
    return fun.call_wrapped(*in_vals)
  elif len(sizes) == 1:
    out_val, out_axis = pmap_transform(fun).call_wrapped(name, in_vals, in_axes)
    return batching.moveaxis(sizes.pop(), out_axis_target, out_axis, out_val)
  else:
    raise TypeError("got inconsistent map dimension sizes: {}".format(sizes))

@lu.transformation
def pmap_transform(name, vals, axes):
  with new_master(PmapTrace) as master:
    trace = PmapTrace(master, core.cur_sublevel())
    in_tracers = map(partial(PmapTracer, trace, name), vals, axes)
    ans = yield in_tracers
    out_tracer = trace.full_raise(ans)
    out_val, out_axis = out_tracer.val, out_tracer.axis
    del master
  yield out_val, out_axis

@lu.transformation_with_aux
def pmap_subtrace(master, name, axes, *vals):
  trace = PmapTrace(master, core.cur_sublevel())
  ans = yield map(partial(PmapTracer, trace, name), vals, axes)
  out_tracer = trace.full_raise(ans)
  out_val, out_axis = out_tracer.val, out_tracer.axis
  yield out_val, out_axis

class PmapTracer(Tracer):
  def __init__(self, trace, name, val, axis):
    self.trace = trace
    self.name = name
    self.val = val
    self.axis = axis

  @property
  def aval(self):
    batched_aval = batching.get_aval(self.val)
    return batching.remove_batch_dim_from_aval(self.axis, batched_aval)

  def unpack(self):
    t = type(self.axis)
    if t is tuple:
      axes = self.axis
    elif t is int:
      axes = [self.axis] * len(self.val)
    elif t is type(None):
      return tuple(self.val)
    else:
      raise TypeError(t)
    return map(partial(PmapTracer, self.trace, self.name), self.val, axes)

  def full_lower(self):
    if self.axis is None:
      return core.full_lower(self.val)
    else:
      return self

class PmapTrace(Trace):
  def pure(self, val):
    return PmapTracer(self, None, val, None)

  def lift(self, val):
    return PmapTracer(self, None, val, None)

  def sublift(self, val):
    return PmapTracer(self, val.name, val.val, val.axis)

  def process_primitive(self, primitive, tracers, params):
    names_in, vals_in, axes_in = unzip3((t.name, t.val, t.axis) for t in tracers)
    if all(axis is None for axis in axes_in):
      return primitive.bind(*vals_in, **params)
    else:
      name = next(name for name in names_in if name is not None)  # all same
      if primitive in pmap_primitive_rules:
        if name == params['axis_name']:
          rule = pmap_primitive_rules[primitive]
          params = {k: params[k] for k in params if k != 'axis_name'}
          val_out, axis_out = rule(vals_in, axes_in, **params)
          return PmapTracer(self, name, val_out, axis_out)
        else:
          return primitive.bind(val, **params)
      else:
        rule = batching.get_primitive_batcher(primitive)
        val_out, axis_out = rule(vals_in, axes_in, **params)
        return PmapTracer(self, name, val_out, axis_out)

  def process_call(self, call_primitive, f, tracers, params):
    names, vals, axes = unzip3((t.name, t.val, t.axis) for t in tracers)
    if all(axis is None for axis in axes):
      return call_primitive.bind(f, *vals, **params)
    else:
      name = next(name for name in names if name is not None)  # all same
      f, axis_out = pmap_subtrace(f, self.master, name, axes)
      val_out = call_primitive.bind(f, *vals, **params)
      return PmapTracer(self, name, val_out, axis_out())

  def post_process_call(self, _, out_tracer):
    raise NotImplementedError  # TODO(mattjj,dougalm)

  def pack(self, tracers):
    vals = pack([t.val for t in tracers])
    axis = tuple(t.axis for t in tracers)
    name = next(t.name for t in tracers if t.name)
    return PmapTracer(self, name, vals, axis)


def unbound_name_error(primitive_name, *args, **kwargs):
  axis_name = kwargs['axis_name']
  msg = "axis name '{}' is unbound for primitive {}."
  raise NameError(msg.format(axis_name, primitive_name))

def PmapPrimitive(name):
  prim = Primitive(name)
  prim.def_impl(partial(unbound_name_error, name))
  prim.def_abstract_eval(lambda x, *args, **kwargs: x)  # default
  return prim


pmap_primitive_rules = {}


def psum(x, axis_name):
  return psum_p.bind(x, axis_name=axis_name)

def psum_pmap_rule(vals, axes):
  val, = vals
  axis, = axes
  return val.sum(axis), None

def psum_parallel_translation_rule(c, in_nodes, device_grp):
  val, = in_nodes
  # return c.CrossReplicaSum(val, device_grp)  # TODO
  return c.CrossReplicaSum(val)

psum_p = PmapPrimitive('psum')
pmap_primitive_rules[psum_p] = psum_pmap_rule
pxla.parallel_translation_rules[psum_p] = psum_parallel_translation_rule


def gather(x, axis_name):
  return gather_p.bind(x, axis_name=axis_name)

def gather_pmap_rule(vals, axes):
  val, = vals
  return val, None

gather_p = PmapPrimitive('gather')
pmap_primitive_rules[gather_p] = gather_pmap_rule


def rescatter(x, new_axis, axis_name):
  return rescatter_p.bind(x, new_axis=new_axis, axis_name=axis_name)

def rescatter_pmap_rule(vals, axes, new_axis):
  val, = vals
  axis, = axes
  raise NotImplementedError  # TODO why the transpose, instead of identity?
  return batching.moveaxis(None, new_axis, axis, val), new_axis

rescatter_p = PmapPrimitive('rescatter')
pmap_primitive_rules[rescatter_p] = rescatter_pmap_rule


# TODO maybe this isn't a great primitive, and it's the only one that needs more
# than one operand at the moment
def _scatter(source, dummy, target_axis, axis_name):
  return scatter_p.bind(source, dummy, target_axis=target_axis, axis_name=axis_name)

def scatter_pmap_rule(vals, axes, target_axis):
  source, _ = vals
  source_axis, _ = axes
  assert source_axis is None
  return source, target_axis

scatter_p = PmapPrimitive('scatter')
pmap_primitive_rules[scatter_p] = scatter_pmap_rule


### papply


newvar = pe.gensym('_axis')

def papply(fun, name, in_vals, in_axes):
  return papply_transform(fun).call_wrapped(name, in_vals, in_axes)

@lu.transformation
def papply_transform(name, args, axes):
  with new_master(PapplyTrace) as master:
    trace = PapplyTrace(master, core.cur_sublevel())
    in_tracers = map(partial(PapplyTracer, trace, name), args, axes)
    out_tracer = yield in_tracers
    out_tracer = trace.full_raise(out_tracer)
    out_val = out_tracer.val
    del master
  yield out_val

class PapplyTracer(Tracer):
  def __init__(self, trace, name, val, axis):
    self.trace = trace
    self.name = name
    self.val = val
    self.axis = axis

  @property
  def aval(self):
    return batching.get_aval(self.val)

  def unpack(self):
    raise NotImplementedError  # TODO

  def full_lower(self):
    if self.axis is None:
      return core.full_lower(self.val)
    else:
      return self

class PapplyTrace(Trace):
  def pure(self, val):
    return PapplyTracer(self, None, val, None)

  def lift(self, val):
    return PapplyTracer(self, None, val, None)

  def sublift(self, val):
    return PapplyTracer(self, val.name, val.val, val.axis)

  def process_primitive(self, primitive, tracers, params):
    names, vals, axes = unzip3((t.name, t.val, t.axis) for t in tracers)
    if all(axis is None for axis in axes):
      return primitive.bind(*vals, **params)
    else:
      name = next(n for n in names if n is not None)
      rule = papply_primitive_rules[primitive]
      val_out, axis_out = rule(name, vals, axes, **params)
      return PapplyTracer(self, name, val_out, axis_out)

  def process_call(self, call_primitive, f, tracers, params):
    raise NotImplementedError  # TODO(mattjj)

  def post_process_call(self, _, out_tracer):
    raise NotImplementedError  # TODO(mattjj)

  def pack(self, tracers):
    vals = core.pack([t.val for t in tracers])
    axis = tuple(t.axis for t in tracers)
    name = tuple(t.name for t in tracers)
    return PapplyTracer(self, name, vals, axis)


papply_primitive_rules = {}


def scatter_like(source, target):
  return scatter_like_p.bind(source, target)

def scatter_like_papply_rule(name, vals, axes):
  source, target = vals
  source_axis, target_axis = axes
  assert source_axis is None
  return _scatter(source, target, target_axis, name)

scatter_like_p = Primitive('scatter_like')
scatter_like_p.def_abstract_eval(lambda source, target: source)
papply_primitive_rules[scatter_like_p] = scatter_like_papply_rule


def defvectorized(prim):
  papply_primitive_rules[prim] = partial(vectorized_papply, prim)

def vectorized_papply(prim, name, vals, axes, **params):
  assert all(axes[0] == a for a in axes[1:])
  return prim.bind(*vals, **params), axes[0]


def defreducer(prim, collective_prim):
  papply_primitive_rules[prim] = partial(reducer_papply, prim, collective_prim)

def reducer_papply(prim, cprim, name, vals, papply_axes, input_shape, axes):
  operand, = vals
  papply_axis, = papply_axes

  other_axes = [i for i in axes if i != papply_axis]
  if other_axes:
    result = prim.bind(operand, axes=other_axes, input_shape=input_shape)
  else:
    result = operand

  if not axes or papply_axis in axes:
    return cprim.bind(result, axis_name=name), None
  else:
    new_papply_axis = papply_axis - onp.sum(onp.less(other_axes, papply_axis))
    return result, new_papply_axis


def defbroadcasting(prim):
  papply_primitive_rules[prim] = partial(broadcasting_papply, prim)

def broadcasting_papply(prim, name, vals, axes, **params):
  x, y = vals
  xdim, ydim = axes

  if xdim is None:
    return prim.bind(x, y, **params), ydim
  elif ydim is None:
    return prim.bind(x, y, **params), xdim
  elif xdim == ydim:
    return prim.bind(x, y, **params), xdim
  else:
    # TODO rescatter based on sizes
    raise NotImplementedError  # this isn't right, need to think about names
    x = rescatter(x, ydim, name)
    return prim.bind(x, y, **params), ydim