import pytest

import jax
import jax.numpy as jnp
from jax import random
import equinox as eqx
import optax
from jax.scipy.stats import multivariate_normal
import jinns


@pytest.fixture
def train_OU_init():
    jax.config.update("jax_enable_x64", False)
    key = random.PRNGKey(2)
    eqx_list = [
        [eqx.nn.Linear, 3, 30],
        [jax.nn.tanh],
        [eqx.nn.Linear, 30, 30],
        [jax.nn.tanh],
        [eqx.nn.Linear, 30, 30],
        [jax.nn.tanh],
        [eqx.nn.Linear, 30, 1],
        [jnp.exp],
    ]
    key, subkey = random.split(key)
    u = jinns.utils.create_PINN(subkey, eqx_list, "nonstatio_PDE", 2)
    rar_parameters = {
        "start_iter": 1000,  # the gradient step at which RAR algo starts (enables a burn in period)
        "update_every": 500,  # nb of gradient steps between two RAR procedures
        "sample_size": 50,  # the size of the sample of collocation points from which the new collocation points will be taken
        "selected_sample_size": 5,  # the number of selected collocation points from the sample, to join the dataset.
    }
    n_start = 200  # the initial number of spatial collocation points at beginning
    nt_start = 150  # the initial number of temporal collocation points at beginning
    init_nn_params = u.init_params()

    n = 500
    nb = 4  # not used here
    nt = 400  # != n
    omega_batch_size = 32
    omega_border_batch_size = None  # not used here
    temporal_batch_size = 20
    xmin = -3
    xmax = 3
    ymin = -3
    ymax = 3
    tmin = 0
    tmax = 1
    Tmax = 10
    method = "uniform"

    key, subkey = random.split(key)
    train_data = jinns.data.CubicMeshPDENonStatio(
        subkey,
        n,
        nb,
        nt,
        omega_batch_size,
        omega_border_batch_size,
        temporal_batch_size,
        2,
        (xmin, ymin),
        (xmax, ymax),
        tmin,
        tmax,
        method=method,
        rar_parameters=rar_parameters,
        n_start=n_start,
        nt_start=nt_start,
    )

    sigma = 0.5 * jnp.ones((2))
    alpha = 0.5 * jnp.ones((2))
    mu = jnp.zeros((2))

    init_params = {
        "nn_params": init_nn_params,
        "eq_params": {"sigma": sigma, "alpha": alpha, "mu": mu},
    }

    def u0(x):
        return multivariate_normal.pdf(x, mean=jnp.array([1, 1]), cov=0.1 * jnp.eye(2))

    int_xmin, int_xmax = -5, 5
    int_ymin, int_ymax = -5, 5

    n_samples = 32
    int_length = (int_xmax - int_xmin) * (int_ymax - int_ymin)
    key, subkey1, subkey2 = random.split(key, 3)
    mc_samples = jnp.concatenate(
        [
            random.uniform(
                subkey1, shape=(n_samples, 1), minval=int_xmin, maxval=int_xmax
            ),
            random.uniform(
                subkey2, shape=(n_samples, 1), minval=int_ymin, maxval=int_ymax
            ),
        ],
        axis=-1,
    )

    loss_weights = {
        "dyn_loss": 1,
        "initial_condition": 1 * Tmax,
        "norm_loss": 0.1 * Tmax,
    }
    OU_fpe_non_statio_2D_loss = jinns.loss.OU_FPENonStatioLoss2D(Tmax=Tmax)

    # Catching an expected UserWarning since no border condition is given
    # for this specific PDE (Fokker-Planck).
    with pytest.warns(UserWarning):
        loss = jinns.loss.LossPDENonStatio(
            u=u,
            loss_weights=loss_weights,
            dynamic_loss=OU_fpe_non_statio_2D_loss,
            initial_condition_fun=u0,
            norm_borders=((int_xmin, int_xmax), (int_ymin, int_ymax)),
            norm_samples=mc_samples,
        )

    return init_params, loss, train_data


@pytest.fixture
def train_OU_10it(train_OU_init):
    """
    Fixture that requests a fixture
    """
    init_params, loss, train_data = train_OU_init

    # NOTE we need to waste one get_batch() here to stay synchronized with the
    # notebook
    _ = loss.evaluate(init_params, train_data.get_batch())[0]

    params = init_params

    tx = optax.adamw(learning_rate=1e-3)
    n_iter = 10
    # Catching an expected UserWarning since no border condition is given
    # for this specific PDE (Fokker-Planck).
    with pytest.warns(UserWarning):
        params, total_loss_list, loss_by_term_dict, _, _, _, _ = jinns.solve(
            init_params=params, data=train_data, optimizer=tx, loss=loss, n_iter=n_iter
        )
    return total_loss_list[9]


def test_initial_loss_OU(train_OU_init):
    init_params, loss, train_data = train_OU_init

    assert jnp.allclose(
        loss.evaluate(init_params, train_data.get_batch())[0], TODO, atol=1e-1
    )


def test_10it_OU(train_OU_10it):
    total_loss_val = train_OU_10it
    assert jnp.allclose(total_loss_val, TODO2, atol=1e-1)


def test_rar_error_with_SPINN(train_OU_init):
    from jinns.utils import create_SPINN

    init_params, loss, train_data = train_OU_init
    d = 3
    r = 256
    eqx_list = [
        [eqx.nn.Linear, 1, 128],
        [jax.nn.tanh],
        [eqx.nn.Linear, 128, 128],
        [jax.nn.tanh],
        [eqx.nn.Linear, 128, 128],
        [jax.nn.tanh],
        [eqx.nn.Linear, 128, r],
    ]
    key = jax.random.PRNGKey(12345)
    key, subkey = random.split(key)
    u = jinns.utils.create_SPINN(subkey, d, r, eqx_list, "nonstatio_PDE")
    init_nn_params = u.init_params()

    # update loss and params
    init_params["nn_params"] = init_nn_params
    loss.u = u
    train_data.temporal_batch_size = train_data.omega_batch_size
    # expect error
    with pytest.raises(NotImplementedError) as e_info, pytest.warns(UserWarning):
        tx = optax.adamw(learning_rate=1e-3)
        jinns.solve(
            init_params=init_params, data=train_data, optimizer=tx, loss=loss, n_iter=2
        )
