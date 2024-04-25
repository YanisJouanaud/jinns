"""
This modules implements the main `solve()` function of jinns which
handles the optimization process
"""

from functools import partial
from typing import NamedTuple, Union
from jaxtyping import PyTree
import optax
import jax
from jax import jit
import jax.numpy as jnp
from jax.typing import ArrayLike
from jinns.solver._seq2seq import trigger_seq2seq, initialize_seq2seq
from jinns.solver._rar import init_rar, trigger_rar
from jinns.utils._utils import _check_nan_in_pytree, _tracked_parameters
from jinns.data._DataGenerators import (
    DataGeneratorODE,
    CubicMeshPDEStatio,
    CubicMeshPDENonStatio,
    DataGeneratorParameter,
    DataGeneratorObservations,
    DataGeneratorObservationsMultiPINNs,
    append_param_batch,
    append_obs_batch,
)


def check_batch_size(other_data, main_data, attr_name):
    if (
        (
            isinstance(main_data, DataGeneratorODE)
            and getattr(other_data, attr_name) != main_data.temporal_batch_size
        )
        or (
            isinstance(main_data, CubicMeshPDEStatio)
            and not isinstance(main_data, CubicMeshPDENonStatio)
            and getattr(other_data, attr_name) != main_data.omega_batch_size
        )
        or (
            isinstance(main_data, CubicMeshPDENonStatio)
            and getattr(other_data, attr_name)
            != main_data.omega_batch_size * main_data.temporal_batch_size
        )
    ):
        raise ValueError(
            "Optional other_data.param_batch_size must be"
            " equal to main_data.temporal_batch_size or main_data.omega_batch_size or"
            " the product of both dependeing on the type of the main"
            " datagenerator"
        )


class DataGeneratorContainer(NamedTuple):
    data: Union[DataGeneratorODE, CubicMeshPDEStatio, CubicMeshPDENonStatio]
    param_data: Union[DataGeneratorParameter, None] = None
    obs_data: Union[
        DataGeneratorObservations, DataGeneratorObservationsMultiPINNs, None
    ] = None


class ValidationContainer(NamedTuple):
    data: DataGeneratorContainer
    hyperparams: PyTree = None


class OptimizationContainer(NamedTuple):
    params: dict
    last_non_nan_params: dict
    opt_state: optax.OptState


class OptimizationExtraContainer(NamedTuple):
    curr_seq: int
    seq2seq: Union[dict, None]
    early_stopping: bool = False


class LossContainer(NamedTuple):
    stored_loss_terms: dict
    train_loss_values: ArrayLike
    validation_loss_values: Union[ArrayLike, None] = None


class StoredObjectContainer(NamedTuple):
    stored_params: Union[list, None]


def solve(
    n_iter,
    init_params,
    data,
    loss,
    optimizer,
    print_loss_every=1000,
    opt_state=None,
    seq2seq=None,
    tracked_params_key_list=None,
    param_data=None,
    obs_data=None,
    validation=None,
    obs_batch_sharding=None,
):
    """
    Performs the optimization process via stochastic gradient descent
    algorithm. We minimize the function defined `loss.evaluate()` with
    respect to the learnable parameters of the problem whose initial values
    are given in `init_params`.


    Parameters
    ----------
    n_iter
        The number of iterations in the optimization
    init_params
        The initial dictionary of parameters. Typically, it is a dictionary of
        dictionaries: `eq_params` and `nn_params``, respectively the
        differential equation parameters and the neural network parameter
    data
        A DataGenerator object which implements a `get_batch()`
        method which returns a 3-tuple with (omega_grid, omega_border, time grid).
        It must be jittable (e.g. implements via a pytree
        registration)
    loss
        A loss object (e.g. a LossODE, SystemLossODE, LossPDEStatio [...]
        object). It must be jittable (e.g. implements via a pytree
        registration)
    optimizer
        An `optax` optimizer (e.g. `optax.adam`).
    print_loss_every
        Integer. Default 100. The rate at which we print the loss value in the
        gradient step loop.
    opt_state
        Default None. Provide an optional initial optional state to the
        optimizer. Not valid for all optimizers.
    seq2seq
        Default None. A dictionary with keys 'times_steps'
        and 'iter_steps' which mush have same length. The first represents
        the time steps which represents the different time interval upon
        which we perform the incremental learning. The second represents
        the number of iteration we perform in each time interval.
        The seq2seq approach we reimplements is defined in
        "Characterizing possible failure modes in physics-informed neural
        networks", A. S. Krishnapriyan, NeurIPS 2021
    tracked_params_key_list
        Default None. Otherwise it is a list of list of strings
        to access a leaf in params. Each selected leaf will be tracked
        and stored at each iteration and returned by the solve function
    param_data
        Default None. A DataGeneratorParameter object which can be used to
        sample equation parameters.
    obs_data
        Default None. A DataGeneratorObservations object which can be used to
        sample minibatches of observations
    validation
        Default None. Otherwise a tuple that enables the set up of a validation
        procedure. The tuple is composed of 5 elements:
            - validation_data: a DataGenerator of the same type of data for validation
            dataset for the collocation points. Can be None
            depending on the validation loss that is used
            - validation_param_data: a DataGenerator of the same type of param_data
            for validation dataset for the collocation points. Can be None
            depending on the validation loss that is used
            - validation_obs_data: a DataGenerator of the same type of obs_data
            for validation dataset for the collocation points. Can be None
            depending on the validation loss that is used
            - fun: a function that is called after every gradient step to
            compute validation quantities with the validation DataGenerator.
            fun must take as arguments:
                - the iteration number
                - a new hyperparams pytree
                - the loss evaluate function: the evaluate bound method to the
                  argument loss defined above. We pass the method and not the
                  object to avoid custom validation loss to modify the loss
                  object
                - params (as init_params defined above)
                - a validation_data
                - a validation_param_data
                - a validation_obs_data
            fun must return 6 variables:
                - a boolean that trigger early stopping if True
                - a float for the validation loss value
                - a validation_data
                - a validation_param_data
                - a validation_obs_data
                - a new hyperparams pytree (see below)
            - hyperparams: a pytree which contains the validation parameters
            that fun needs to function
    obs_batch_sharding
        Default None. An optional sharding object to constraint the obs_batch.
        Typically, a SingleDeviceSharding(gpu_device) when obs_data has been
        created with sharding_device=SingleDeviceSharding(cpu_device) to avoid
        loading on GPU huge datasets of observations

    Returns
    -------
    params
        The last non NaN value of the dictionaries of parameters at then end of the
        optimization process
    total_loss_values
        An array of the total loss term along the gradient steps
    stored_loss_terms
        A dictionary. At each key an array of the values of a given loss
        term is stored
    data
        The input data object
    loss
        The input loss object
    opt_state
        The final optimized state
    stored_params
        A dictionary. At each key an array of the values of the parameters
        given in tracked_params_key_list is stored
    """
    if param_data is not None:
        check_batch_size(param_data, data, "param_batch_size")

    if obs_data is not None:
        check_batch_size(obs_data, data, "obs_batch_size")

    # TODO add checks for the validation tuple

    if opt_state is None:
        opt_state = optimizer.init(init_params)

    # RAR sampling init (ouside scanned function to avoid dynamic slice error)
    # If RAR is not used the _rar_step_*() are juste None and data is unchanged
    data, _rar_step_true, _rar_step_false = init_rar(data)

    # Seq2seq
    curr_seq = 0
    if seq2seq is not None:
        assert (
            data.method == "uniform"
        ), "data.method must be uniform if using seq2seq learning !"
        data, opt_state = initialize_seq2seq(loss, data, seq2seq, opt_state)

    train_loss_values = jnp.zeros((n_iter))
    if validation is not None:
        validation_loss_values = jnp.zeros((n_iter))
    else:
        validation_loss_values = None
    # depending on obs_batch_sharding we will get the simple get_batch or the
    # get_batch with device_put, the latter is not jittable
    get_batch = get_get_batch(obs_batch_sharding)

    # initialize the dict for stored parameter values
    # we need to get a loss_term to init stuff
    batch_ini, data, param_data, obs_data = get_batch(data, param_data, obs_data)
    _, loss_terms = loss(init_params, batch_ini)
    if tracked_params_key_list is None:
        tracked_params_key_list = []
    tracked_params = _tracked_parameters(init_params, tracked_params_key_list)
    stored_params = jax.tree_util.tree_map(
        lambda tracked_param, param: (
            jnp.zeros((n_iter,) + param.shape) if tracked_param else None
        ),
        tracked_params,
        init_params,
    )

    # initialize the dict for stored loss values
    stored_loss_terms = jax.tree_util.tree_map(
        lambda x: jnp.zeros((n_iter)), loss_terms
    )

    train_data = DataGeneratorContainer(
        data=data, param_data=param_data, obs_data=obs_data
    )
    if validation is not None:
        validation_step = validation[3]  # grab the validation fun argument
        validation = ValidationContainer(
            data=DataGeneratorContainer(
                data=validation[0], param_data=validation[1], obs_data=validation[2]
            ),
            hyperparams=validation[4],
        )
    optimization = OptimizationContainer(
        params=init_params, last_non_nan_params=init_params.copy(), opt_state=opt_state
    )
    optimization_extra = OptimizationExtraContainer(
        curr_seq=curr_seq,
        seq2seq=seq2seq,
        early_stopping=False,
    )
    loss_container = LossContainer(
        stored_loss_terms=stored_loss_terms,
        train_loss_values=train_loss_values,
        validation_loss_values=validation_loss_values,
    )
    stored_objects = StoredObjectContainer(
        stored_params=stored_params,
    )

    break_fun = get_break_fun(n_iter)

    iteration = 0
    carry = (
        iteration,
        loss,
        optimization,
        optimization_extra,
        train_data,
        validation,
        loss_container,
        stored_objects,
    )

    def one_iteration(carry):
        (
            i,
            loss,
            optimization,
            optimization_extra,
            train_data,
            validation,
            loss_container,
            stored_objects,
        ) = carry

        batch, data, param_data, obs_data = get_batch(
            train_data.data, train_data.param_data, train_data.obs_data
        )

        # Gradient step
        (
            loss,
            train_loss_value,
            loss_terms,
            params,
            opt_state,
            last_non_nan_params,
        ) = gradient_step(
            loss,
            optimizer,
            batch,
            optimization.params,
            optimization.opt_state,
            optimization.last_non_nan_params,
        )

        # Print train loss value during optimization
        print_fn(i, train_loss_value, print_loss_every, prefix="[train] ")

        # Validation step
        if validation is not None:
            # there is a jax.lax.cond because we do not necesarily call the
            # validation step every iteration
            (
                early_stopping,
                validation_loss_value,
                validation_data,
                validation_param_data,
                validation_obs_data,
                validation_hyperparams,
            ) = jax.lax.cond(
                i % validation.hyperparams.call_every == 0,
                lambda operands: validation_step(
                    *operands[:2], loss.evaluate, *operands[2:]
                ),
                lambda _: (
                    optimization_extra.early_stopping,
                    loss_container.validation_loss_values[i - 1],
                    *validation.data,
                    validation.hyperparams,
                ),
                (
                    i,
                    validation.hyperparams,
                    params,
                    *validation.data,
                ),
            )
            # Print validation loss value during optimization
            print_fn(i, validation_loss_value, print_loss_every, prefix="[validation] ")
        else:
            early_stopping = False
            validation_loss_value = None

        # Trigger RAR
        loss, params, data = trigger_rar(
            i, loss, params, data, _rar_step_true, _rar_step_false
        )

        # Trigger seq2seq
        loss, params, data, opt_state, curr_seq, seq2seq = trigger_seq2seq(
            i,
            loss,
            params,
            data,
            opt_state,
            optimization_extra.curr_seq,
            optimization_extra.seq2seq,
        )

        # save loss value and selected parameters
        stored_params, stored_loss_terms, train_loss_values, validation_loss_values = (
            store_loss_and_params(
                i,
                params,
                stored_objects.stored_params,
                loss_container.stored_loss_terms,
                loss_container.train_loss_values,
                loss_container.validation_loss_values,
                train_loss_value,
                validation_loss_value,
                loss_terms,
                tracked_params,
            )
        )
        i += 1

        return (
            i,
            loss,
            OptimizationContainer(params, last_non_nan_params, opt_state),
            OptimizationExtraContainer(curr_seq, seq2seq, early_stopping),
            DataGeneratorContainer(data, param_data, obs_data),
            (
                ValidationContainer(
                    DataGeneratorContainer(
                        data=validation_data,
                        param_data=validation_param_data,
                        obs_data=validation_obs_data,
                    ),
                    validation_hyperparams,
                )
                if validation is not None
                else validation
            ),
            LossContainer(stored_loss_terms, train_loss_values, validation_loss_values),
            StoredObjectContainer(stored_params),
        )

    # Main optimization loop. We use the LAX while loop (fully jitted) version
    # if no mixing devices. Otherwise we use the standard while loop. Here devices only
    # concern obs_batch, but it could lead to more complex scheme in the future
    if obs_batch_sharding is not None:
        while break_fun(carry):
            carry = one_iteration(carry)
    else:
        carry = jax.lax.while_loop(break_fun, one_iteration, carry)

    (
        i,
        loss,
        optimization,
        optimization_extra,
        train_data,
        validation,
        loss_container,
        stored_objects,
    ) = carry

    jax.debug.print(
        "Final iteration {i}: train loss value = {train_loss_val}",
        i=i,
        train_loss_val=loss_container.train_loss_values[i - 1],
    )
    if validation_loss_values is not None:
        jax.debug.print(
            "validation loss value = {validation_loss_val}",
            validation_loss_val=loss_container.validation_loss_values[i - 1],
        )

    if validation is None:
        return (
            optimization.last_non_nan_params,
            loss_container.train_loss_values,
            loss_container.stored_loss_terms,
            train_data.data,
            loss,
            optimization.opt_state,
            stored_objects.stored_params,
        )
    else:
        return (
            optimization.last_non_nan_params,
            loss_container.train_loss_values,
            loss_container.stored_loss_terms,
            train_data.data,
            loss,
            optimization.opt_state,
            stored_objects.stored_params,
            loss_container.validation_loss_values,
        )


@partial(jit, static_argnames=["optimizer"])
def gradient_step(loss, optimizer, batch, params, opt_state, last_non_nan_params):
    """
    optimizer cannot be jit-ted.
    """
    value_grad_loss = jax.value_and_grad(loss, has_aux=True)
    (loss_val, loss_terms), grads = value_grad_loss(params, batch)
    updates, opt_state = optimizer.update(grads, opt_state, params)
    params = optax.apply_updates(params, updates)

    # check if any of the parameters is NaN
    last_non_nan_params = jax.lax.cond(
        _check_nan_in_pytree(params),
        lambda _: last_non_nan_params,
        lambda _: params,
        None,
    )

    return (
        loss,
        loss_val,
        loss_terms,
        params,
        opt_state,
        last_non_nan_params,
    )


@partial(jit, static_argnames=["prefix"])
def print_fn(i, loss_val, print_loss_every, prefix=""):
    # note that if the following is not jitted in the main lor loop, it is
    # super slow
    _ = jax.lax.cond(
        i % print_loss_every == 0,
        lambda _: jax.debug.print(
            prefix + "Iteration {i}: loss value = {loss_val}",
            i=i,
            loss_val=loss_val,
        ),
        lambda _: None,
        (None,),
    )


@jit
def store_loss_and_params(
    i,
    params,
    stored_params,
    stored_loss_terms,
    train_loss_values,
    validation_loss_values,
    train_loss_val,
    validation_loss_val,
    loss_terms,
    tracked_params,
):
    stored_params = jax.tree_util.tree_map(
        lambda stored_value, param, tracked_param: jax.lax.cond(
            tracked_param,
            lambda ope: ope[0].at[i].set(ope[1]),
            lambda ope: ope[0],
            (stored_value, param),
        ),
        stored_params,
        params,
        tracked_params,
    )
    stored_loss_terms = jax.tree_util.tree_map(
        lambda stored_term, loss_term: stored_term.at[i].set(loss_term),
        stored_loss_terms,
        loss_terms,
    )

    train_loss_values = train_loss_values.at[i].set(train_loss_val)
    if validation_loss_values is not None:
        validation_loss_values = validation_loss_values.at[i].set(validation_loss_val)
    return (stored_params, stored_loss_terms, train_loss_values, validation_loss_values)


def get_break_fun(n_iter):
    """
    Wrapper to get the break_fun with appropriate `n_iter`
    """

    @jit
    def break_fun(carry):
        """
        Function to break from the main optimization loop
        We check several conditions
        """

        def stop_while_loop(msg):
            """
            Note that the message is wrapped in the jax.lax.cond because a
            string is not a valid JAX type that can be fed into the operands
            """
            jax.debug.print(f"Stopping main optimization loop, cause: {msg}")
            return False

        def continue_while_loop(_):
            return True

        (i, _, optimization, optimization_extra, _, _, _, _) = carry

        # Condition 1
        bool_max_iter = jax.lax.cond(
            i >= n_iter,
            lambda _: stop_while_loop("max iteration is reached"),
            continue_while_loop,
            None,
        )
        # Condition 2
        bool_nan_in_params = jax.lax.cond(
            _check_nan_in_pytree(optimization.params),
            lambda _: stop_while_loop(
                "NaN values in parameters " "(returning last non NaN values)"
            ),
            continue_while_loop,
            None,
        )
        # Condition 3
        bool_early_stopping = jax.lax.cond(
            optimization_extra.early_stopping,
            lambda _: stop_while_loop("early stopping"),
            continue_while_loop,
            _,
        )

        # stop when one of the cond to continue is False
        return jax.tree_util.tree_reduce(
            lambda x, y: jnp.logical_and(jnp.array(x), jnp.array(y)),
            (bool_max_iter, bool_nan_in_params, bool_early_stopping),
        )

    return break_fun


def get_get_batch(obs_batch_sharding):
    """
    Return the get_batch function that will be used either the jittable one or
    the non-jittable one with sharding
    """

    def get_batch_sharding(data, param_data, obs_data):
        """
        This function is used at each loop but it cannot be jitted because of
        device_put

        Note: return all that's modified or unwanted dirty undefined behaviour
        """
        batch = data.get_batch()
        if param_data is not None:
            batch = append_param_batch(batch, param_data.get_batch())
        if obs_data is not None:
            # This is the part that motivated the transition from scan to for loop
            # Indeed we need to be transit obs_batch from CPU to GPU when we have
            # huge observations that cannot fit on GPU. Such transfer wasn't meant
            # to be jitted, i.e. in a scan loop
            obs_batch = jax.device_put(obs_data.get_batch(), obs_batch_sharding)
            batch = append_obs_batch(batch, obs_batch)
        return batch, data, param_data, obs_data

    @jit
    def get_batch(data, param_data, obs_data):
        """
        Original get_batch with not sharding

        Note: return all that's modified or unwanted dirty undefined behaviour
        """
        batch = data.get_batch()
        if param_data is not None:
            batch = append_param_batch(batch, param_data.get_batch())
        if obs_data is not None:
            batch = append_obs_batch(batch, obs_data.get_batch())
        return batch, data, param_data, obs_data

    if obs_batch_sharding is not None:
        return get_batch_sharding
    return get_batch
