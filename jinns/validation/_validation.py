"""
Implements some validation functions and their associated hyperparameter
"""

from typing import NamedTuple, Union
import jax
import jax.numpy as jnp
from jax.typing import ArrayLike
from jinns.data._DataGenerators import (
    append_param_batch,
    append_obs_batch,
)
from jinns.utils._containers import ValidationContainer


class ValidationLossEarlyStoppingHyperparams(NamedTuple):
    """
    User must set the patience value and the call_every attributes only
    """

    call_every: int  # Mandatory for all validation step, tells that the
    # validation step is performed every call_every iterations
    patience: Union[int, None]
    best_val_loss: float = jnp.inf
    counter: int = 0  # counts the number of times we did not improve validation loss


def eval_validation_loss_and_early_stopping(
    i: int, params: dict, validation: ValidationContainer
):
    """
    The simplest validation loss to implement early stopping

    hyperparams is of type ValidationLossEarlyStoppingHyperparams
    """
    val_batch = validation.data.data.get_batch()
    if validation.data.param_data is not None:
        val_batch = append_param_batch(
            val_batch, validation.data.param_data.get_batch()
        )
    if validation.data.obs_data is not None:
        val_batch = append_obs_batch(val_batch, validation.data.obs_data.get_batch())
    validation_loss_value, _ = validation.loss(params, val_batch)

    hyperparams = validation.hyperparams
    (counter, best_val_loss) = jax.lax.cond(
        jnp.logical_and(
            jnp.array(i > 0),
            jnp.array(validation_loss_value < hyperparams.best_val_loss),
        ),
        lambda operands: (0, validation_loss_value),
        lambda operands: (operands[0] + 1, operands[1]),
        (hyperparams.counter, hyperparams.best_val_loss),
    )
    hyperparams = hyperparams._replace(counter=counter)
    hyperparams = hyperparams._replace(best_val_loss=best_val_loss)
    validation = validation._replace(hyperparams=hyperparams)

    validation = validation._replace(
        loss_values=validation.loss_values.at[i].set(validation_loss_value)
    )

    bool_early_stopping = jax.lax.cond(
        hyperparams.counter == hyperparams.patience,
        lambda _: True,
        lambda _: False,
        None,
    )

    return bool_early_stopping, validation
