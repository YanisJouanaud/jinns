import numpy as np
import jax
import jax.numpy as jnp
import optax
import equinox as eqx
from functools import reduce
from operator import getitem


def _check_nan_in_pytree(pytree):
    """
    Check if there is a NaN value anywhere is the pytree

    Parameters
    ----------
    pytree
        A pytree

    Returns
    -------
    res
        A boolean. True if any of the pytree content is NaN
    """
    return jnp.any(
        jnp.array(
            [
                value
                for value in jax.tree_util.tree_leaves(
                    jax.tree_util.tree_map(lambda x: jnp.any(jnp.isnan(x)), pytree)
                )
            ]
        )
    )


def _tracked_parameters(params, tracked_params_key_list):
    """
    Returns a pytree with the same structure as params with True is the
    parameter is tracked False otherwise
    """

    def set_nested_item(dataDict, mapList, val):
        """
        Set item in nested dictionary
        https://stackoverflow.com/questions/54137991/how-to-update-values-in-nested-dictionary-if-keys-are-in-a-list
        """
        reduce(getitem, mapList[:-1], dataDict)[mapList[-1]] = val
        return dataDict

    tracked_params = jax.tree_util.tree_map(
        lambda x: False, params
    )  # init with all False

    for key_list in tracked_params_key_list:
        tracked_params = set_nested_item(tracked_params, key_list, True)

    return tracked_params


class _MLP(eqx.Module):
    """
    Class to construct an equinox module from a key and a eqx_list. To be used
    in pair with the function `create_PINN`
    """

    layers: list

    def __init__(self, key, eqx_list):
        """
        Parameters
        ----------
        key
            A jax random key
        eqx_list
            A list of list of successive equinox modules and activation functions to
            describe the PINN architecture. The inner lists have the eqx module or
            axtivation function as first item, other items represents arguments
            that could be required (eg. the size of the layer).
            __Note:__ the `key` argument need not be given.
            Thus typical example is `eqx_list=
            [[eqx.nn.Linear, 2, 20],
                [jax.nn.tanh],
                [eqx.nn.Linear, 20, 20],
                [jax.nn.tanh],
                [eqx.nn.Linear, 20, 20],
                [jax.nn.tanh],
                [eqx.nn.Linear, 20, 1]
            ]`
        """

        self.layers = []
        # TODO we are limited currently in the number of layer type we can
        # parse and we lack some safety checks
        for l in eqx_list:
            if len(l) == 1:
                self.layers.append(l[0])
            else:
                # By default we append a random key at the end of the
                # arguments fed into a layer module call
                key, subkey = jax.random.split(key, 2)
                # the argument key is keyword only
                self.layers.append(l[0](*l[1:], key=subkey))

    def __call__(self, t):
        for layer in self.layers:
            t = layer(t)
        return t


class PINN:
    """
    Basically a wrapper around the `__call__` function to be able to give a type to
    our former `self.u`
    The function create_PINN has the role to population the `__call__` function
    """

    def __init__(self, key, eqx_list):
        _pinn = _MLP(key, eqx_list)
        self.params, self.static = eqx.partition(_pinn, eqx.is_inexact_array)

    def init_params(self):
        return self.params


def create_PINN(
    key,
    eqx_list,
    eq_type,
    dim_x=0,
    with_eq_params=None,
    input_transform=None,
    output_transform=None,
):
    """
    Utility function to create a standard PINN neural network with the equinox
    library.

    Parameters
    ----------
    key
        A jax random key that will be used to initialize the network parameters
    eqx_list
        A list of list of successive equinox modules and activation functions to
        describe the PINN architecture. The inner lists have the eqx module or
        axtivation function as first item, other items represents arguments
        that could be required (eg. the size of the layer).
        __Note:__ the `key` argument need not be given.
        Thus typical example is `eqx_list=
        [[eqx.nn.Linear, 2, 20],
        [jax.nn.tanh],
        [eqx.nn.Linear, 20, 20],
        [jax.nn.tanh],
        [eqx.nn.Linear, 20, 20],
        [jax.nn.tanh],
        [eqx.nn.Linear, 20, 1]
        ]`
    eq_type
        A string with three possibilities.
        "ODE": the PINN is called with one input `t`.
        "statio_PDE": the PINN is called with one input `x`, `x`
        can be high dimensional.
        "nonstatio_PDE": the PINN is called with two inputs `t` and `x`, `x`
        can be high dimensional.
        **Note: the input dimension as given in eqx_list has to match the sum
        of the dimension of `t` + the dimension of `x` + the number of
        parameters in `eq_params` if with_eq_params is `True` (see below)**
    dim_x
        An integer. The dimension of `x`. Default `0`
    with_eq_params
        Default is None. Otherwise a list of keys from the dict `eq_params`
        that  the network also takes as inputs.
        the equation parameters (`eq_params`).
        **If some keys are provided, the input dimension
        as given in eqx_list must take into account the number of such provided
        keys (i.e., the input dimension is the addition of the dimension of ``t``
        + the dimension of ``x`` + the number of ``eq_params``)**
    input_transform
        A function that will be called before entering the PINN. Its output(s)
        must mathc the PINN inputs.
    output_transform
        A function with arguments the same input(s) as the PINN AND the PINN
        output that will be called after exiting the PINN


    Returns
    -------
    init_fn
        A function which (re-)initializes the PINN parameters with the provided
        jax random key
    apply_fn
        A function to apply the neural network on given inputs for given
        parameters. A typical call will be of the form `u(t, nn_params)` for
        ODE or `u(t, x, nn_params)` for nD PDEs (`x` being multidimensional)
        or even `u(t, x, nn_params, eq_params)` if with_eq_params is `True`

    Raises
    ------
    RuntimeError
        If the parameter value for eq_type is not in `["ODE", "statio_PDE",
        "nonstatio_PDE"]`
    RuntimeError
        If we have a `dim_x > 0` and `eq_type == "ODE"`
        or if we have a `dim_x = 0` and `eq_type != "ODE"`
    """
    if eq_type not in ["ODE", "statio_PDE", "nonstatio_PDE"]:
        raise RuntimeError("Wrong parameter value for eq_type")

    if eq_type == "ODE" and dim_x != 0:
        raise RuntimeError("Wrong parameter combination eq_type and dim_x")

    if eq_type != "ODE" and dim_x == 0:
        raise RuntimeError("Wrong parameter combination eq_type and dim_x")

    dim_t = 0 if eq_type == "statio_PDE" else 1
    dim_in_params = len(with_eq_params) if with_eq_params is not None else 0
    try:
        nb_inputs_declared = eqx_list[0][1]  # normally we look for 2nd ele of 1st layer
    except IndexError:
        nb_inputs_declared = eqx_list[1][1]
        # but we can have, eg, a flatten first layer

    # NOTE Currently the check below is disabled because we added
    # input_transform
    # if dim_t + dim_x + dim_in_params != nb_inputs_declared:
    #    raise RuntimeError("Error in the declarations of the number of parameters")

    if eq_type == "ODE":
        if with_eq_params is None:

            def apply_fn(self, t, u_params, eq_params=None):
                model = eqx.combine(u_params, self.static)
                t = t[
                    None
                ]  # Note that we added a dimension to t which is lacking for the ODE batches
                if output_transform is None:
                    if input_transform is not None:
                        return model(input_transform(t)).squeeze()
                    else:
                        return model(t).squeeze()
                else:
                    if input_transform is not None:
                        return output_transform(t, model(input_transform(t)).squeeze())
                    else:
                        return output_transform(t, model(t).squeeze())

        else:

            def apply_fn(self, t, u_params, eq_params):
                model = eqx.combine(u_params, self.static)
                t = t[
                    None
                ]  # We added a dimension to t which is lacking for the ODE batches
                eq_params_flatten = jnp.concatenate(
                    [e.ravel() for k, e in eq_params.items() if k in with_eq_params]
                )
                t_eq_params = jnp.concatenate([t, eq_params_flatten], axis=-1)

                if output_transform is None:
                    if input_transform is not None:
                        return model(input_transform(t_eq_params)).squeeze()
                    else:
                        return model(t_eq_params).squeeze()
                else:
                    if input_transform is not None:
                        return output_transform(
                            t_eq_params,
                            model(input_transform(t_eq_params)).squeeze(),
                        )
                    else:
                        return output_transform(
                            t_eq_params, model(t_eq_params).squeeze()
                        )

    elif eq_type == "statio_PDE":
        # Here we add an argument `x` which can be high dimensional
        if with_eq_params is None:

            def apply_fn(self, x, u_params, eq_params=None):
                model = eqx.combine(u_params, self.static)

                if output_transform is None:
                    if input_transform is not None:
                        return model(input_transform(x)).squeeze()
                    else:
                        return model(x).squeeze()
                else:
                    if input_transform is not None:
                        return output_transform(x, model(input_transform(x)).squeeze())
                    else:
                        return output_transform(x, model(x).squeeze())

        else:

            def apply_fn(self, x, u_params, eq_params):
                model = eqx.combine(u_params, self.static)
                eq_params_flatten = jnp.concatenate(
                    [e.ravel() for k, e in eq_params.items() if k in with_eq_params]
                )
                x_eq_params = jnp.concatenate([x, eq_params_flatten], axis=-1)

                if output_transform is None:
                    if input_transform is not None:
                        return model(input_transform(x_eq_params)).squeeze()
                    else:
                        return model(x_eq_params).squeeze()
                else:
                    if input_transform is not None:
                        return output_transform(
                            x_eq_params,
                            model(input_transform(x_eq_params)).squeeze(),
                        )
                    else:
                        return output_transform(
                            x_eq_params, model(x_eq_params).squeeze()
                        )

    elif eq_type == "nonstatio_PDE":
        # Here we add an argument `x` which can be high dimensional
        if with_eq_params is None:

            def apply_fn(self, t, x, u_params, eq_params=None):
                model = eqx.combine(u_params, self.static)
                t_x = jnp.concatenate([t, x], axis=-1)

                if output_transform is None:
                    if input_transform is not None:
                        return model(input_transform(t_x)).squeeze()
                    else:
                        return model(t_x).squeeze()
                else:
                    if input_transform is not None:
                        return output_transform(
                            t_x, model(input_transform(t_x)).squeeze()
                        )
                    else:
                        return output_transform(t_x, model(t_x).squeeze())

        else:

            def apply_fn(self, t, x, u_params, eq_params):
                model = eqx.combine(u_params, self.static)
                t_x = jnp.concatenate([t, x], axis=-1)
                eq_params_flatten = jnp.concatenate(
                    [e.ravel() for k, e in eq_params.items() if k in with_eq_params]
                )
                t_x_eq_params = jnp.concatenate([t_x, eq_params_flatten], axis=-1)

                if output_transform is None:
                    if input_transform is not None:
                        return model(input_transform(t_x_eq_params)).squeeze()
                    else:
                        return model(t_x_eq_params).squeeze()
                else:
                    if input_transform is not None:
                        return output_transform(
                            t_x_eq_params,
                            model(input_transform(t_x_eq_params)).squeeze(),
                        )
                    else:
                        return output_transform(
                            t_x_eq_params,
                            model(input_transform(t_x_eq_params)).squeeze(),
                        )

    else:
        raise RuntimeError("Wrong parameter value for eq_type")

    PINN.__call__ = apply_fn

    return PINN(key, eqx_list)


class _SPINN(eqx.Module):
    """
    Construct a Separable PINN as proposed in
    Cho et al., _Separable Physics-Informed Neural Networks_, NeurIPS, 2023
    """

    layers: list
    separated_mlp: list
    d: int
    r: int

    def __init__(self, key, d, r, eqx_list):
        """
        Parameters
        ----------
        key
            A jax random key
        d
            An integer. The number of dimensions to treat separately
        r
            An integer. The dimension of the embedding
        eqx_list
            A list of list of successive equinox modules and activation functions to
            describe *each separable PINN architecture*.
            The inner lists have the eqx module or
            axtivation function as first item, other items represents arguments
            that could be required (eg. the size of the layer).
            __Note:__ the `key` argument need not be given.
            Thus typical example is `eqx_list=
            [[eqx.nn.Linear, 2, 20],
                [jax.nn.tanh],
                [eqx.nn.Linear, 20, 20],
                [jax.nn.tanh],
                [eqx.nn.Linear, 20, 20],
                [jax.nn.tanh],
                [eqx.nn.Linear, 20, 1]
            ]`
        """
        keys = jax.random.split(key, 8)

        self.d = d
        self.r = r

        self.separated_mlp = []
        for d in range(self.d):
            self.layers = []
            for l in eqx_list:
                if len(l) == 1:
                    self.layers.append(l[0])
                else:
                    key, subkey = jax.random.split(key, 2)
                    self.layers.append(l[0](*l[1:], key=subkey))
            self.separated_mlp.append(self.layers)

    def __call__(self, t, x):
        if t is not None:
            dimensions = jnp.concatenate([t, x.flatten()], axis=0)
        else:
            dimensions = jnp.concatenate([x.flatten()], axis=0)
        outputs = []
        for d in range(self.d):
            t_ = dimensions[d][None]
            for layer in self.separated_mlp[d]:
                t_ = layer(t_)
            outputs += [t_]
        return jnp.asarray(outputs)


class SPINN:
    """
    Basically a wrapper around the `__call__` function to be able to give a type to
    our former `self.u`
    The function create_SPINN has the role to population the `__call__` function
    """

    def __init__(self, key, d, r, eqx_list):
        _spinn = _SPINN(key, d, r, eqx_list)
        self.params, self.static = eqx.partition(_spinn, eqx.is_inexact_array)

    def init_params(self):
        return self.params


def create_SPINN(
    key,
    d,
    r,
    eqx_list,
    eq_type,
    with_eq_params=None,
    input_transform=None,
    output_transform=None,
):
    """
    Utility function to create a SPINN neural network with the equinox
    library.

    *Note* that a SPINN is not vmapped from the outside and expects batch of the
    same size for each input. It outputs directly a solution of shape
    (batchsize, batchsize). See the paper for more details.

    Parameters
    ----------
    key
        A jax random key that will be used to initialize the network parameters
    d
        An integer. The number of dimensions to treat separately
    r
        An integer. The dimension of the embedding
    eqx_list
        A list of list of successive equinox modules and activation functions to
        describe *each separable PINN architecture*.
        The inner lists have the eqx module or
        axtivation function as first item, other items represents arguments
        that could be required (eg. the size of the layer).
        __Note:__ the `key` argument need not be given.
        Thus typical example is `eqx_list=
        [[eqx.nn.Linear, 2, 20],
        [jax.nn.tanh],
        [eqx.nn.Linear, 20, 20],
        [jax.nn.tanh],
        [eqx.nn.Linear, 20, 20],
        [jax.nn.tanh],
        [eqx.nn.Linear, 20, 1]
        ]`
    eq_type
        A string with three possibilities.
        "ODE": the PINN is called with one input `t`.
        "statio_PDE": the PINN is called with one input `x`, `x`
        can be high dimensional.
        "nonstatio_PDE": the PINN is called with two inputs `t` and `x`, `x`
        can be high dimensional.
        **Note: the input dimension as given in eqx_list has to match the sum
        of the dimension of `t` + the dimension of `x` + the number of
        parameters in `eq_params` if with_eq_params is `True` (see below)**
    with_eq_params
        TODO
    input_transform
        TODO
    output_transform
        TODO


    Returns
    -------
    init_fn
        A function which (re-)initializes the SPINN parameters with the provided
        jax random key
    apply_fn
        A function to apply the neural network on given inputs for given
        parameters. A typical call will be of the form `u(t, nn_params)` for
        ODE or `u(t, x, nn_params)` for nD PDEs (`x` being multidimensional)
        or even `u(t, x, nn_params, eq_params)` if with_eq_params is `True`

    Raises
    ------
    RuntimeError
        If the parameter value for eq_type is not in `["ODE", "statio_PDE",
        "nonstatio_PDE"]` and for various failing checks
    """

    if eq_type not in ["ODE", "statio_PDE", "nonstatio_PDE"]:
        raise RuntimeError("Wrong parameter value for eq_type")

    try:
        nb_inputs_declared = eqx_list[0][1]  # normally we look for 2nd ele of 1st layer
    except IndexError:
        nb_inputs_declared = eqx_list[1][
            1
        ]  # but we can have, eg, a flatten first layer
    if nb_inputs_declared != 1:
        raise ValueError("Input dim must be set to 1 in SPINN!")

    try:
        nb_outputs_declared = eqx_list[-1][2]  # normally we look for 3rd ele of
        # last layer
    except IndexError:
        nb_outputs_declared = eqx_list[-2][2]
        # but we can have, eg, a `jnp.exp` last layer
    if nb_outputs_declared != r:
        raise ValueError("Output dim must be set to r in SPINN!")

    if d > 24:
        raise ValueError(
            "Too many dimensions, not enough letters" " available in jnp.einsum"
        )

    if eq_type == "statio_PDE":

        def apply_fn(self, x, u_params, eq_params=None):
            spinn = eqx.combine(u_params, self.static)
            v_model = jax.vmap(spinn, ((0, 0)))
            res = v_model(t=None, x=x)
            # We prepare an outer product for an arbitrary nb of 2D arrays
            # (outer product on first dim and summation on second (embedding)
            # dim)
            a = ", ".join([f"{chr(97 + d)}z" for d in range(res.shape[1])])
            b = "".join([f"{chr(97 + d)}" for d in range(res.shape[1])])
            res = jnp.einsum(f"{a} -> {b}", *(res[:, d] for d in range(res.shape[1])))
            return res

    elif eq_type == "nonstatio_PDE":

        def apply_fn(self, t, x, u_params, eq_params=None):
            spinn = eqx.combine(u_params, self.static)
            # NOTE we tried the nested vmap to get the outer product
            # differently and enable different batch sizes for different
            # dimensions (see eg
            # https://stackoverflow.com/questions/73212780/struggling-to-understand-nested-vmaps-in-jax),
            # but it is much slower than the following...
            v_model = jax.vmap(spinn, ((0, 0)))
            res = v_model(t, x)
            a = ", ".join([f"{chr(97 + d)}z" for d in range(res.shape[1])])
            b = "".join([f"{chr(97 + d)}" for d in range(res.shape[1])])
            res = jnp.einsum(f"{a} -> {b}", *(res[:, d] for d in range(res.shape[1])))
            return res

    else:
        raise RuntimeError("Wrong parameter value for eq_type")

    SPINN.__call__ = apply_fn

    return SPINN(key, d, r, eqx_list)

    # def make_spinn(key, d, r, eqx_list):
    #    spinn = _SPINN(key, d, r, eqx_list)
    #    params, static = eqx.partition(spinn, eqx.is_inexact_array)

    #    def init_fn():
    #        return params

    #    if eq_type == "nonstatio_PDE":

    #        def apply_fn(t, x, u_params, eq_params=None):
    #            model = eqx.combine(u_params, static)
    #            v_model = jax.vmap(model, ((0, 0)))
    #            r = v_model(t, x)
    #            r = jnp.einsum('ik, jk -> ij', r[:, 0], r[:, 1])
    #            return r

    #    return init_fn, apply_fn

    # return make_spinn(key, d, r, eqx_list)


def _get_grid(in_array):
    """
    From an array of shape (B, D), D > 1, get the grid array, i.e., an array of
    shape (B, B, ...(D times)..., B, D): along the last axis we have the array
    of values
    """
    if in_array.shape[-1] > 1 or in_array.ndim > 1:
        return jnp.stack(
            jnp.meshgrid(
                *(in_array[..., d] for d in range(in_array.shape[-1])), indexing="ij"
            ),
            axis=-1,
        )
    else:
        return in_array


def _get_vmap_in_axes_params(eq_params_batch_dict, params):
    """
    Return the input vmap axes when there is batch(es) of parameters to vmap
    over. The latter are designated by keys in eq_params_batch_dict
    If eq_params_batch_dict (ie no additional parameter batch), we return None
    """
    if eq_params_batch_dict is None:
        return (None,)
    else:
        # We use pytree indexing of vmapped axes and vmap on axis
        # 0 of the eq_parameters for which we have a batch
        # this is for a fine-grained vmaping
        # scheme over the params
        vmap_in_axes_params = (
            {
                "eq_params": {
                    k: (0 if k in eq_params_batch_dict.keys() else None)
                    for k in params["eq_params"].keys()
                },
                "nn_params": None,
            },
        )
        return vmap_in_axes_params


def alternate_optax_solver(
    steps, parameters_set1, parameters_set2, lr_set1, lr_set2, label_fn=None
):
    """
    This function creates an optax optimizer that alternates the optimization
    between two set of parameters (ie. when some parameters are update to a
    given learning rates, others are not updated (learning rate = 0)
    The optimizers are scaled by adam parameters.

    __Note:__ The alternating pattern relies on
    `optax.piecewise_constant_schedule` which __multiplies__ learning rates of
    previous steps (current included) to set the new learning rate. Hence, our
    strategy used here is to relying on potentially cancelling power of tens to
    create the alternating scheme.

    Parameters
    ----------
    steps
        An array which describes the epochis number at which we alternate the
        optimization: the parameter_set that is being updated now stops
        updating, the other parameter_set starts updating.
        __Note:__ The step 0 should not be included
    parameters_set1
        A list of leaf level keys which must be found in the general `params` dict. The
        parameters in this `set1` will be the parameters which are updated
        first in the alternating scheme.
    parameters_set2
        A list of leaf level keys which must be found in the general `params` dict. The
        parameters in this `set2` will be the parameters which are not updated
        first in the alternating scheme.
    lr_set1
        A float. The learning rate of updates for set1.
    lr_set2
        A float. The learning rate of updates for set2.
    label_fn
        The same function as the label_fn function passed in an optax
        `multi_transform`
        [https://optax.readthedocs.io/en/latest/api.html#optax.multi_transform](see
        here)
        Default None, ie, we already internally provide the default one (as
        proposed in the optax documentation) which may suit many use cases

    Returns
    -------
    tx
        The optax optimizer object
    """

    def map_nested_fn(fn):
        """
        Recursively apply `fn` to the key-value pairs of a nested dict
        We follow the example from
        https://optax.readthedocs.io/en/latest/api.html#optax.multi_transform
        for different learning rates
        """

        def map_fn(nested_dict):
            return {
                k: (map_fn(v) if isinstance(v, dict) else fn(k, v))
                for k, v in nested_dict.items()
            }

        return map_fn

    label_fn = map_nested_fn(lambda k, _: k)

    power_to_0 = 1e-25  # power of ten used to force a learning rate to 0
    power_to_lr = 1 / power_to_0  # power of ten used to force a learning rate to lr
    nn_params_scheduler = optax.piecewise_constant_schedule(
        init_value=lr_set1,
        boundaries_and_scales={
            k: (
                power_to_0
                if even_odd % 2 == 0  # set lr to 0 eg if even_odd is even ie at
                # first step
                else power_to_lr
            )
            for even_odd, k in enumerate(steps)
        },
    )
    eq_params_scheduler = optax.piecewise_constant_schedule(
        init_value=power_to_0 * lr_set2,  # so normal learning rate is 1e-3
        boundaries_and_scales={
            k: (power_to_lr if even_odd % 2 == 0 else power_to_0)
            for even_odd, k in enumerate(steps)
        },
    )

    # the scheduler for set1 is called nn_chain because we usually start by
    # updating the NN parameters
    nn_chain = optax.chain(
        optax.scale_by_adam(),
        optax.scale_by_schedule(nn_params_scheduler),
        optax.scale(-1.0),
    )
    eq_chain = optax.chain(
        optax.scale_by_adam(),
        optax.scale_by_schedule(eq_params_scheduler),
        optax.scale(-1.0),
    )
    dict_params_set1 = {p: nn_chain for p in parameters_set1}
    dict_params_set2 = {p: eq_chain for p in parameters_set2}
    tx = optax.multi_transform(
        {**dict_params_set1, **dict_params_set2},
        label_fn,
    )

    return tx


def euler_maruyama_density(t, x, s, y, params, Tmax=1):
    eps = 1e-6
    delta = jnp.abs(t - s) * Tmax
    mu = params["alpha_sde"] * (params["mu_sde"] - y) * delta
    var = params["sigma_sde"] ** 2 * delta
    return (
        1 / jnp.sqrt(2 * jnp.pi * var) * jnp.exp(-0.5 * ((x - y) - mu) ** 2 / var) + eps
    )


def log_euler_maruyama_density(t, x, s, y, params):
    eps = 1e-6
    delta = jnp.abs(t - s)
    mu = params["alpha_sde"] * (params["mu_sde"] - y) * delta
    logvar = params["logvar_sde"]
    return (
        -0.5
        * (jnp.log(2 * jnp.pi * delta) + logvar + ((x - y) - mu) ** 2 / jnp.exp(logvar))
        + eps
    )


def euler_maruyama(x0, alpha, mu, sigma, T, N):
    """
    Simulate 1D diffusion process with simple parametrization using the Euler
    Maruyama method in the interval [0, T]
    """
    path = [np.array([x0])]

    time_steps, step_size = np.linspace(0, T, N, retstep=True)
    for i in time_steps[1:]:
        path.append(
            path[-1]
            + step_size * (alpha * (mu - path[-1]))
            + sigma * np.random.normal(loc=0.0, scale=np.sqrt(step_size))
        )

    return time_steps, np.stack(path)
