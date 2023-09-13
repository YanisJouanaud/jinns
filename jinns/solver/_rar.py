import jax
from jax import vmap
import jax.numpy as jnp
from jinns.data._DataGenerators import (
    DataGeneratorODE,
    CubicMeshPDEStatio,
    CubicMeshPDENonStatio,
)
from jinns.loss._LossPDE import LossPDEStatio, LossPDENonStatio, SystemLossPDE
from jinns.loss._LossODE import LossODE, SystemLossODE
from functools import partial


def rar_step_init(sample_size, selected_sample_size):
    """
    This is a wrapper because the sampling size and
    selected_sample_size, must be treated static
    in order to slice. So they must be set before jitting and not with the jitted
    dictionary values rar["test_points_nb"] and rar["added_points_nb"]

    This is a kind of manual declaration of static argnums
    """

    def rar_step_true(operands):
        loss, params, data, i = operands

        if isinstance(data, DataGeneratorODE):
            s = data.sample_in_time_domain(sample_size)

            # We can have different types of Loss
            if isinstance(loss, LossODE):
                v_dyn_loss = vmap(
                    lambda t: loss.dynamic_loss.evaluate(t, loss.u, params),
                    (0),
                    0,
                )
                dyn_on_s = v_dyn_loss(s)
                if dyn_on_s.ndim > 1:
                    mse_on_s = (jnp.linalg.norm(dyn_on_s, axis=-1) ** 2).flatten()
                else:
                    mse_on_s = dyn_on_s**2
            elif isinstance(loss, SystemLossODE):
                mse_on_s = 0

                for i in loss.dynamic_loss_dict.keys():
                    v_dyn_loss = vmap(
                        lambda t: loss.dynamic_loss_dict[i].evaluate(
                            t, loss.u_dict, params
                        ),
                        (0),
                        0,
                    )
                    dyn_on_s = v_dyn_loss(s)
                    if dyn_on_s.ndim > 1:
                        mse_on_s += (jnp.linalg.norm(dyn_on_s, axis=-1) ** 2).flatten()
                    else:
                        mse_on_s += dyn_on_s**2

            ## Select the m points with higher dynamic loss
            higher_residual_idx = jax.lax.dynamic_slice(
                jnp.argsort(mse_on_s),
                (mse_on_s.shape[0] - selected_sample_size,),
                (selected_sample_size,),
            )
            higher_residual_points = s[higher_residual_idx]

            data.rar_iter_from_last_sampling = 0

            ## add the new points in times
            # start indices of update can be dynamic but the the shape (length)
            # of the slice
            data.times = jax.lax.dynamic_update_slice(
                data.times,
                higher_residual_points,
                (data.nt_start + data.rar_iter_nb * selected_sample_size,),
            )

            ## rearrange probabilities so that the probabilities of the new
            ## points are non-zero
            new_proba = 1 / (data.nt_start + data.rar_iter_nb * selected_sample_size)
            # the next work because nt_start is static
            data.p = data.p.at[: data.nt_start].set(new_proba)

            # the next requires a fori_loop because the range is dynamic
            def update_slices(i, p):
                return jax.lax.dynamic_update_slice(
                    p,
                    1 / new_proba * jnp.ones((selected_sample_size,)),
                    ((data.nt_start + (i + 1) * selected_sample_size),),
                )

            data.p = jax.lax.fori_loop(0, data.rar_iter_nb, update_slices, data.p)

            data.rar_iter_nb += 1

            # NOTE must return data to be correctly updated because we cannot
            # have side effects in this function that will be jitted
            return data

        elif isinstance(data, CubicMeshPDEStatio) and not isinstance(
            data, CubicMeshPDENonStatio
        ):
            s = data.sample_in_omega_domain(sample_size)

            # We can have different types of Loss
            if isinstance(loss, LossPDEStatio):
                v_dyn_loss = vmap(
                    lambda x: loss.dynamic_loss.evaluate(
                        x,
                        loss.u,
                        params,
                    ),
                    (0),
                    0,
                )
                dyn_on_s = v_dyn_loss(s)
                if dyn_on_s.ndim > 1:
                    mse_on_s = (jnp.linalg.norm(dyn_on_s, axis=-1) ** 2).flatten()
                else:
                    mse_on_s = dyn_on_s**2
            elif isinstance(loss, SystemLossPDE):
                mse_on_s = 0
                for i in loss.dynamic_loss_dict.keys():
                    # only the case LossPDEStatio here
                    v_dyn_loss = vmap(
                        lambda x: loss.dynamic_loss_dict[i].evaluate(
                            x, loss.u_dict, params
                        ),
                        0,
                        0,
                    )
                    dyn_on_s = v_dyn_loss(s)
                    if dyn_on_s.ndim > 1:
                        mse_on_s += (jnp.linalg.norm(dyn_on_s, axis=-1) ** 2).flatten()
                    else:
                        mse_on_s += dyn_on_s**2

            ## Select the m points with higher dynamic loss
            higher_residual_idx = jax.lax.dynamic_slice(
                jnp.argsort(mse_on_s),
                (mse_on_s.shape[0] - selected_sample_size,),
                (selected_sample_size,),
            )
            higher_residual_points = s[higher_residual_idx]

            data.rar_iter_from_last_sampling = 0

            ## add the new points in times
            # start indices of update can be dynamic but the the shape (length)
            # of the slice
            data.omega = jax.lax.dynamic_update_slice(
                data.omega,
                higher_residual_points,
                (data.n_start + data.rar_iter_nb * selected_sample_size, data.dim),
            )

            ## rearrange probabilities so that the probabilities of the new
            ## points are non-zero
            new_proba = 1 / (data.n_start + data.rar_iter_nb * selected_sample_size)
            # the next work because n_start is static
            data.p = data.p.at[: data.n_start].set(new_proba)

            # the next requires a fori_loop because the range is dynamic
            def update_slices(i, p):
                return jax.lax.dynamic_update_slice(
                    p,
                    1 / new_proba * jnp.ones((selected_sample_size,)),
                    ((data.n_start + (i + 1) * selected_sample_size),),
                )

            data.p = jax.lax.fori_loop(0, data.rar_iter_nb, update_slices, data.p)

            data.rar_iter_nb += 1

            # NOTE must return data to be correctly updated because we cannot
            # have side effects in this function that will be jitted
            return data

        elif isinstance(data, CubicMeshPDENonStatio):
            st = data.sample_in_time_domain(sample_size)
            sx = data.sample_in_omega_domain(sample_size)

            # According to the Loss type we have different syntax to call the
            # dynamic_loss evaluate function
            if isinstance(loss, LossPDEStatio) and not isinstance(
                loss, LossPDENonStatio
            ):
                # This case might not happen very often...
                v_dyn_loss = vmap(
                    lambda x: loss.dynamic_loss.evaluate(
                        x,
                        loss.u,
                        params,
                    ),
                    (0),
                    0,
                )
                dyn_on_s = v_dyn_loss(sx)
                if dyn_on_s.ndim > 1:
                    mse_on_s = (jnp.linalg.norm(dyn_on_s, axis=-1) ** 2).flatten()
                else:
                    mse_on_s = dyn_on_s**2
            elif isinstance(loss, LossPDENonStatio):
                v_dyn_loss = vmap(
                    lambda t, x: loss.dynamic_loss.evaluate(t, x, loss.u, params),
                    (0, 0),
                    0,
                )
                dyn_on_s = v_dyn_loss(st[..., None], sx)
                if dyn_on_s.ndim > 1:
                    mse_on_s = (jnp.linalg.norm(dyn_on_s, axis=-1) ** 2).flatten()
                else:
                    mse_on_s = dyn_on_s**2
            elif isinstance(loss, SystemLossPDE):
                mse_on_s = 0
                for i in loss.dynamic_loss_dict.keys():
                    if isinstance(loss.dynamic_loss_dict[i], PDEStatio):
                        v_dyn_loss = vmap(
                            lambda x: loss.dynamic_loss_dict[i].evaluate(
                                x, loss.u_dict, params
                            ),
                            0,
                            0,
                        )
                        dyn_on_s = v_dyn_loss(sx)
                        if dyn_on_s.ndim > 1:
                            mse_on_s += (
                                jnp.linalg.norm(dyn_on_s, axis=-1) ** 2
                            ).flatten()
                        else:
                            mse_on_s += dyn_on_s**2
                    else:
                        v_dyn_loss = vmap(
                            lambda t, x: loss.dynamic_loss_dict[i].evaluate(
                                t, x, loss.u_dict, params
                            ),
                            (0, 0),
                            0,
                        )
                        dyn_on_s = v_dyn_loss(st[..., None], sx)
                        if dyn_on_s.ndim > 1:
                            mse_on_s += (
                                jnp.linalg.norm(dyn_on_s, axis=-1) ** 2
                            ).flatten()
                        else:
                            mse_on_s += dyn_on_s**2

            ## Now that we have the residuals, select the m points
            # with higher dynamic loss (residuals)
            higher_residual_idx = jax.lax.dynamic_slice(
                jnp.argsort(mse_on_s),
                (mse_on_s.shape[0] - selected_sample_size,),
                (selected_sample_size,),
            )
            higher_residual_points_st = st[higher_residual_idx]
            higher_residual_points_sx = sx[higher_residual_idx]

            data.rar_iter_from_last_sampling = 0

            ## add the new points in times
            # start indices of update can be dynamic but the the shape (length)
            # of the slice
            data.times = jax.lax.dynamic_update_slice(
                data.times,
                higher_residual_points_st,
                (data.n_start + data.rar_iter_nb * selected_sample_size,),
            )

            ## add the new points in omega
            data.omega = jax.lax.dynamic_update_slice(
                data.omega,
                higher_residual_points_sx,
                (
                    data.n_start + data.rar_iter_nb * selected_sample_size,
                    data.dim,
                ),
            )

            ## rearrange probabilities so that the probabilities of the new
            ## points are non-zero
            new_proba = 1 / (data.n_start + data.rar_iter_nb * selected_sample_size)
            # the next work because nt_start is static
            data.p = data.p.at[: data.n_start].set(new_proba)

            # the next requires a fori_loop because the range is dynamic
            def update_slices(i, p):
                return jax.lax.dynamic_update_slice(
                    p,
                    1 / new_proba * jnp.ones((selected_sample_size,)),
                    ((data.n_start + (i + 1) * selected_sample_size),),
                )

            data.p = jax.lax.fori_loop(0, data.rar_iter_nb, update_slices, data.p)

            data.rar_iter_nb += 1

            # NOTE must return data to be correctly updated because we cannot
            # have side effects in this function that will be jitted
            return data

    def rar_step_false(operands):
        loss_evaluate_fun, params, data, i = operands

        # Add 1 only if we are after the burn in period
        data.rar_iter_from_last_sampling = jax.lax.cond(
            i < data.rar_parameters["start_iter"],
            lambda operand: 0,
            lambda operand: operand + 1,
            (data.rar_iter_from_last_sampling),
        )

        return data

    return rar_step_true, rar_step_false