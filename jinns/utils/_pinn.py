"""
Implements utility function to create PINNs
"""

import jax
import jax.numpy as jnp
import equinox as eqx


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

    def __init__(
        self,
        key,
        eqx_list,
        dim_solution,
        eq_type,
        input_transform,
        output_transform,
        output_slice=None,
    ):
        _pinn = _MLP(key, eqx_list)
        self.params, self.static = eqx.partition(_pinn, eqx.is_inexact_array)
        self.dim_solution = dim_solution
        self.eq_type = eq_type
        self.input_transform = input_transform
        self.output_transform = output_transform
        self.output_slice = output_slice

    def init_params(self):
        return self.params

    def __call__(self, *args):
        if self.eq_type == "ODE":
            (t, params) = args
            t = t[None]  #  Add dimension which is lacking for the ODE batches
            return self._eval_nn(
                t, params, self.input_transform, self.output_transform
            ).squeeze()
        if self.eq_type == "statio_PDE":
            (x, params) = args
            return self._eval_nn(x, params, self.input_transform, self.output_transform)
        if self.eq_type == "nonstatio_PDE":
            (t, x, params) = args
            t_x = jnp.concatenate([t, x], axis=-1)
            return self._eval_nn(
                t_x, params, self.input_transform, self.output_transform
            )
        raise ValueError("Wrong value for self.eq_type")

    def _eval_nn(self, inputs, params, input_transform, output_transform):
        """
        inner function to factorize code. apply_fn (which takes varying forms)
        call _eval_nn which always have the same content.
        """
        try:
            model = eqx.combine(params["nn_params"], self.static)
        except KeyError:  # give more flexibility
            model = eqx.combine(params, self.static)
        res = output_transform(inputs, model(input_transform(inputs, params)).squeeze())

        if self.output_slice is not None:
            res = res[self.output_slice]

        ## force (1,) output for non vectorial solution (consistency)
        if not res.shape:
            return jnp.expand_dims(res, axis=-1)
        return res


def create_PINN(
    key,
    eqx_list,
    eq_type,
    dim_x=0,
    input_transform=None,
    output_transform=None,
    shared_pinn_outputs=None,
    dim_solution=None,
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
        **Note**: the input dimension as given in eqx_list has to match the sum
        of the dimension of `t` + the dimension of `x` or the output dimension
        after the `input_transform` function
    dim_x
        An integer. The dimension of `x`. Default `0`
    input_transform
        A function that will be called before entering the PINN. Its output(s)
        must match the PINN inputs. Its inputs are the PINN inputs (`t` and/or
        `x` concatenated together and the parameters). Default is the No operation
    output_transform
        A function with arguments the same input(s) as the PINN AND the PINN
        output that will be called after exiting the PINN. Default is the No
        operation
    shared_pinn_outputs
        A tuple of jnp.s_[] (slices) to determine the different output for each
        network. In this case we return a list of PINNs, one for each output in
        shared_pinn_outputs. This is useful to create PINNs that share the
        same network and same parameters. Default is None, we only return one PINN.
    dim_solution
        The dimension of the solution that this PINN represents. Default None
        means that dim_solution = the pinn output dim. This argument is useful
        when the pinn is also used to output equation parameters for example


    Returns
    -------
    init_fn
        A function which (re-)initializes the PINN parameters with the provided
        jax random key
    apply_fn
        A function to apply the neural network on given inputs for given
        parameters. A typical call will be of the form `u(t, params)` for
        ODE or `u(t, x, params)` for nD PDEs (`x` being multidimensional)

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

    try:
        nb_outputs_declared = eqx_list[-1][2]  # normally we look for 3rd ele of
        # last layer
    except IndexError:
        nb_outputs_declared = eqx_list[-2][2]

    if dim_solution is None:
        dim_solution = nb_outputs_declared

    if input_transform is None:

        def input_transform(_in, _params):
            return _in

    if output_transform is None:

        def output_transform(_in_pinn, _out_pinn):
            return _out_pinn

    if shared_pinn_outputs is not None:
        pinns = []
        static = None
        for output_slice in shared_pinn_outputs:
            pinn = PINN(
                key,
                eqx_list,
                dim_solution,
                eq_type,
                input_transform,
                output_transform,
                output_slice,
            )
            # all the pinns are in fact the same so we share the same static
            if static is None:
                static = pinn.static
            else:
                pinn.static = static
            pinns.append(pinn)
        return pinns
    pinn = PINN(key, eqx_list, dim_solution, eq_type, input_transform, output_transform)
    return pinn
