""" klujax: a KLU solver for JAX """

__version__ = "0.0.6"
__author__ = "Floris Laporte"

__all__ = ["solve", "coo_mul_vec"]

## IMPORTS

from time import time
from functools import partial

import jax
import numpy as np

jax.config.update("jax_enable_x64", True)
jax.config.update("jax_platform_name", "cpu")

import jax.numpy as jnp
from jax import abstract_arrays, core, lax
from jax.interpreters import ad, batching, xla
from jax.lib import xla_client

import klujax_cpp

## CONSTANTS

COMPLEX_DTYPES = (
    np.complex64,
    np.complex128,
    np.complex256,
    jnp.complex64,
    jnp.complex128,
)

## PRIMITIVES

solve_f64 = core.Primitive("solve_f64")
solve_c128 = core.Primitive("solve_c128")
coo_mul_vec_f64 = core.Primitive("coo_mul_vec_f64")
coo_mul_vec_c128 = core.Primitive("coo_mul_vec_c128")


## EXTRA DECORATORS


def xla_register_cpu(primitive, cpp_fun):
    name = primitive.name.encode()

    def decorator(fun):
        xla_client.register_cpu_custom_call_target(
            name,
            cpp_fun(),
        )
        xla.backend_specific_translations["cpu"][primitive] = partial(fun, name)
        return fun

    return decorator


def ad_register(primitive):
    def decorator(fun):
        ad.primitive_jvps[primitive] = fun
        return fun

    return decorator


def transpose_register(primitive):
    def decorator(fun):
        ad.primitive_transposes[primitive] = fun
        return fun

    return decorator


def vmap_register(primitive, operation):
    def decorator(fun):
        batching.primitive_batchers[primitive] = partial(fun, operation)
        return fun

    return decorator


## IMPLEMENTATIONS


@solve_f64.def_impl
@solve_c128.def_impl
@coo_mul_vec_f64.def_impl
@coo_mul_vec_c128.def_impl
def coo_vec_operation_impl(Ai, Aj, Ax, b):
    raise NotImplementedError


## ABSTRACT EVALUATIONS


@solve_f64.def_abstract_eval
@solve_c128.def_abstract_eval
@coo_mul_vec_f64.def_abstract_eval
@coo_mul_vec_c128.def_abstract_eval
def coo_vec_operation_impl(Ai, Aj, Ax, b):
    return abstract_arrays.ShapedArray(b.shape, b.dtype)


# ENABLE JIT


@xla_register_cpu(solve_f64, klujax_cpp.solve_f64)
@xla_register_cpu(solve_c128, klujax_cpp.solve_c128)
@xla_register_cpu(coo_mul_vec_f64, klujax_cpp.coo_mul_vec_f64)
@xla_register_cpu(coo_mul_vec_c128, klujax_cpp.coo_mul_vec_c128)
def coo_vec_operation_xla(primitive_name, c, Ai, Aj, Ax, b):
    Ax_shape = c.get_shape(Ax)
    Ai_shape = c.get_shape(Ai)
    Aj_shape = c.get_shape(Aj)
    b_shape = c.get_shape(b)
    *_n_lhs_list, _Anz = Ax_shape.dimensions()
    assert len(_n_lhs_list) < 2, "solve alows for maximum one batch dimension."
    _n_lhs = np.prod(np.array(_n_lhs_list, np.int32))
    Ax = xla_client.ops.Reshape(Ax, (_n_lhs * _Anz,))
    Ax_shape = c.get_shape(Ax)
    if _n_lhs_list:
        _n_lhs_b, _n_col, *_n_rhs_list = b_shape.dimensions()
    else:
        _n_col, *_n_rhs_list = b_shape.dimensions()
        _n_lhs_b = 1
    assert _n_lhs_b == _n_lhs, "Batch dimension of Ax and b don't match."
    _n_rhs = np.prod(np.array(_n_rhs_list, dtype=np.int32))
    b = xla_client.ops.Reshape(b, (_n_lhs, _n_col, _n_rhs))
    b = xla_client.ops.Transpose(b, (0, 2, 1))
    b = xla_client.ops.Reshape(b, (_n_lhs * _n_rhs * _n_col,))
    b_shape = c.get_shape(b)
    Anz = xla_client.ops.ConstantLiteral(c, np.int32(_Anz))
    n_col = xla_client.ops.ConstantLiteral(c, np.int32(_n_col))
    n_rhs = xla_client.ops.ConstantLiteral(c, np.int32(_n_rhs))
    n_lhs = xla_client.ops.ConstantLiteral(c, np.int32(_n_lhs))
    Anz_shape = xla_client.Shape.array_shape(np.dtype(np.int32), (), ())
    n_col_shape = xla_client.Shape.array_shape(np.dtype(np.int32), (), ())
    n_lhs_shape = xla_client.Shape.array_shape(np.dtype(np.int32), (), ())
    n_rhs_shape = xla_client.Shape.array_shape(np.dtype(np.int32), (), ())
    result = xla_client.ops.CustomCallWithLayout(
        c,
        primitive_name,
        operands=(n_col, n_lhs, n_rhs, Anz, Ai, Aj, Ax, b),
        operand_shapes_with_layout=(
            n_col_shape,
            n_lhs_shape,
            n_rhs_shape,
            Anz_shape,
            Ai_shape,
            Aj_shape,
            Ax_shape,
            b_shape,
        ),
        shape_with_layout=b_shape,
    )
    result = xla_client.ops.Reshape(result, (_n_lhs, _n_rhs, _n_col))
    result = xla_client.ops.Transpose(result, (0, 2, 1))
    if _n_lhs_list:
        result = xla_client.ops.Reshape(result, (_n_lhs, _n_col, *_n_rhs_list))
    else:
        result = xla_client.ops.Reshape(result, (_n_col, *_n_rhs_list))
    return result


# ENABLE FORWARD GRAD


@ad_register(solve_f64)
def solve_f64_value_and_jvp(arg_values, arg_tangents):
    # A x - b = 0
    # ∂A x + A ∂x - ∂b = 0
    # ∂x = A^{-1} (∂b - ∂A x)
    Ai, Aj, Ax, b = arg_values
    dAi, dAj, dAx, db = arg_tangents
    dAx = dAx if not isinstance(dAx, ad.Zero) else lax.zeros_like_array(Ax)
    dAi = dAi if not isinstance(dAi, ad.Zero) else lax.zeros_like_array(Ai)
    dAj = dAj if not isinstance(dAj, ad.Zero) else lax.zeros_like_array(Aj)
    db = db if not isinstance(db, ad.Zero) else lax.zeros_like_array(b)

    x = solve(Ai, Aj, Ax, b)
    dA_x = coo_mul_vec(Ai, Aj, dAx, x)
    dx = solve(Ai, Aj, Ax, db)  # - dA_x)

    return x, dx


# ENABLE BACKWARD GRAD


@transpose_register(solve_f64)
def solve_f64_transpose(ct, Ai, Aj, Ax, b):
    assert ad.is_undefined_primal(b)
    ct_b = solve(Ai, Aj, Ax, ct)  # probably not correct...
    return None, None, None, ct_b


## THE FUNCTIONS


@jax.jit  # jitting by default allows for empty implementation definitions
def solve(Ai, Aj, Ax, b):
    if any(x.dtype in COMPLEX_DTYPES for x in (Ax, b)):
        result = solve_c128.bind(
            Ai.astype(jnp.int32),
            Aj.astype(jnp.int32),
            Ax.astype(jnp.complex128),
            b.astype(jnp.complex128),
        )
    else:
        result = solve_f64.bind(
            Ai.astype(jnp.int32),
            Aj.astype(jnp.int32),
            Ax.astype(jnp.float64),
            b.astype(jnp.float64),
        )
    return result


@jax.jit  # jitting by default allows for empty implementation definitions
def coo_mul_vec(Ai, Aj, Ax, b):
    if any(x.dtype in COMPLEX_DTYPES for x in (Ax, b)):
        result = coo_mul_vec_c128.bind(
            Ai.astype(jnp.int32),
            Aj.astype(jnp.int32),
            Ax.astype(jnp.complex128),
            b.astype(jnp.complex128),
        )
    else:
        result = coo_mul_vec_f64.bind(
            Ai.astype(jnp.int32),
            Aj.astype(jnp.int32),
            Ax.astype(jnp.float64),
            b.astype(jnp.float64),
        )
    return result


# ENABLE VMAP


@vmap_register(solve_f64, solve)
@vmap_register(solve_c128, solve)
@vmap_register(coo_mul_vec_f64, coo_mul_vec)
@vmap_register(coo_mul_vec_c128, coo_mul_vec)
def coo_vec_operation_vmap(operation, vector_arg_values, batch_axes):
    aAi, aAj, aAx, ab = batch_axes
    Ai, Aj, Ax, b = vector_arg_values

    assert aAi is None, "Ai cannot be vectorized."
    assert aAj is None, "Aj cannot be vectorized."

    if aAx is not None and ab is not None:
        assert isinstance(aAx, int) and isinstance(ab, int)
        n_lhs = Ax.shape[aAx]
        if ab != 0:
            Ax = jnp.moveaxis(Ax, aAx, 0)
        if ab != 0:
            b = jnp.moveaxis(b, ab, 0)
        result = operation(Ai, Aj, Ax, b)
        return result, 0

    if ab is None:
        assert isinstance(aAx, int)
        n_lhs = Ax.shape[aAx]
        if aAx != 0:
            Ax = jnp.moveaxis(Ax, aAx, 0)
        b = jnp.broadcast_to(b[None], (Ax.shape[0], *b.shape))
        result = operation(Ai, Aj, Ax, b)
        return result, 0

    if aAx is None:
        assert isinstance(ab, int)
        if ab != 0:
            b = jnp.moveaxis(b, ab, 0)
        n_lhs, n_col, *n_rhs_list = b.shape
        n_rhs = np.prod(np.array(n_rhs_list, dtype=np.int32))
        b = b.reshape(n_lhs, n_col, n_rhs).transpose((1, 0, 2)).reshape(n_col, -1)
        result = operation(Ai, Aj, Ax, b)
        result = result.reshape(n_col, n_lhs, *n_rhs_list)
        return result, 1

    raise ValueError("invalid arguments for vmap")


# TEST SOME STUFF

if __name__ == "__main__":
    A = jnp.array(
        [
            [2 + 3j, 3, 0, 0, 0],
            [3, 0, 4, 0, 6],
            [0, -1, -3, 2, 0],
            [0, 0, 1, 0, 0],
            [0, 4, 2, 0, 1],
        ],
        dtype=jnp.complex128,
    )
    A = jnp.array(
        [
            [2, 3, 0, 0, 0],
            [3, 0, 4, 0, 6],
            [0, -1, -3, 2, 0],
            [0, 0, 1, 0, 0],
            [0, 4, 2, 0, 1],
        ],
        dtype=jnp.float64,
    )
    b = jnp.array([[8], [45], [-3], [3], [19]], dtype=jnp.float64)
    b = jnp.array([[8, 7], [45, 44], [-3, -4], [3, 2], [19, 18]], dtype=jnp.float64)
    b = jnp.array([3 + 8j, 8 + 45j, 23 + -3j, -7 - 3j, 13 + 19j], dtype=jnp.complex128)
    b = jnp.array([8, 45, -3, 3, 19], dtype=jnp.float64)
    Ai, Aj = jnp.where(abs(A) > 0)
    Ax = A[Ai, Aj]

    t = time()
    result = solve(Ai, Aj, Ax, b)
    print(f"{time()-t:.3e}", result)

    t = time()
    result = solve(Ai, Aj, Ax, b)
    print(f"{time()-t:.3e}", result)

    t = time()
    result = solve(Ai, Aj, Ax, b)
    print(f"{time()-t:.3e}", result)

    def solve_sum(Ai, Aj, Ax, b):
        return solve(Ai, Aj, Ax, b).sum()

    solve_sum_grad = jax.grad(solve_sum, 2)
    t = time()
    result = solve_sum_grad(Ai, Aj, Ax, b)
    print(f"{time()-t:.3e}", result)
