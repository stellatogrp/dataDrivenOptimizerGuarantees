from functools import partial

import jax.numpy as jnp
from jax import random

from opt_guarantees.algo_steps import (
    k_steps_eval_alista,
    k_steps_eval_ista,
    k_steps_train_alista,
)
from opt_guarantees.l2o_model import L2Omodel
from opt_guarantees.utils.nn_utils import calculate_pinsker_penalty, compute_single_param_KL


class ALISTAmodel(L2Omodel):
    def __init__(self, **kwargs):
        super(ALISTAmodel, self).__init__(**kwargs)

    def initialize_algo(self, input_dict):
        self.factor_static = None
        self.algo = 'alista'
        self.factors_required = False
        self.q_mat_train, self.q_mat_test = input_dict['b_mat_train'], input_dict['b_mat_test']
        D, W = input_dict['D'], input_dict['W']
        # lambd = input_dict['lambd']
        # ista_step = input_dict['ista_step']
        self.D, self.W = D, W
        self.m, self.n = D.shape
        self.output_size = self.n

        evals, evecs = jnp.linalg.eigh(D.T @ D)
        # step = 1 / evals.max()
        lambd = 0.1
        self.ista_step = lambd / evals.max()

        self.k_steps_train_fn = partial(k_steps_train_alista, D=D, W=W,
                                        jit=self.jit)
        self.k_steps_eval_fn = partial(k_steps_eval_alista, D=D, W=W,
                                       jit=self.jit)
        self.out_axes_length = 5

    def init_params(self):
        self.mean_params = jnp.ones((self.train_unrolls, 2))

        # # initialize with ista values
        # # alista_step = alista_cfg['step']
        # # alista_eta = alista_cfg['eta']
        # # self.mean_params = self.mean_params.at[:, 0].set(alista_step)
        # # self.mean_params = self.mean_params.at[:, 1].set(alista_eta)

        self.sigma_params = -jnp.ones((self.train_unrolls, 2)) * 10

        # initialize the prior
        self.prior_param = jnp.log(self.init_var) * jnp.ones(2)

        self.params = [self.mean_params, self.sigma_params, self.prior_param]

    def create_end2end_loss_fn(self, bypass_nn, diff_required):
        supervised = self.supervised and diff_required
        loss_method = self.loss_method

        def predict(params, input, q, iters, z_star, key, factor):
            z0 = jnp.zeros(z_star.size)

            if self.train_fn is not None:
                train_fn = self.train_fn
            else:
                train_fn = self.k_steps_train_fn
            if self.eval_fn is not None:
                eval_fn = self.eval_fn
            else:
                eval_fn = self.k_steps_eval_fn

            # w_key = random.split(key)
            w_key = random.PRNGKey(key)
            perturb = random.normal(w_key, (self.train_unrolls, 2))
            # return scale * random.normal(w_key, (n, m))
            if self.deterministic:
                stochastic_params = params[0]
            else:
                stochastic_params = params[0] + \
                    jnp.sqrt(jnp.exp(params[1])) * perturb

            if bypass_nn:
                eval_out = k_steps_eval_ista(k=iters,
                                             z0=z0,
                                             q=q,
                                             lambd=0.1,
                                             A=self.D,
                                             ista_step=self.ista_step,
                                             supervised=True,
                                             z_star=z_star,
                                             jit=True)
                z_final, iter_losses, z_all_plus_1 = eval_out[0], eval_out[1], eval_out[2]
                angles = None
            else:
                if diff_required:
                    z_final, iter_losses = train_fn(k=iters,
                                                    z0=z0,
                                                    q=q,
                                                    params=stochastic_params,
                                                    supervised=supervised,
                                                    z_star=z_star)
                else:
                    eval_out = eval_fn(k=iters,
                                       z0=z0,
                                       q=q,
                                       params=stochastic_params,
                                       supervised=supervised,
                                       z_star=z_star)
                    z_final, iter_losses, z_all_plus_1 = eval_out[0], eval_out[1], eval_out[2]
                    angles = None

            loss = self.final_loss(loss_method, z_final,
                                   iter_losses, supervised, z0, z_star)

            penalty_loss = calculate_pinsker_penalty(
                self.N_train, params, self.b, self.c, self.delta)
            loss = loss + self.penalty_coeff * penalty_loss

            if diff_required:
                return loss
            else:
                return_out = (loss, iter_losses, z_all_plus_1,
                              angles) + eval_out[3:]
                return return_out
        loss_fn = self.predict_2_loss(predict, diff_required)
        return loss_fn

    def calculate_total_penalty(self, N_train, params, c, b, delta):
        # priors are already rounded
        rounded_priors = params[2]

        # second: calculate the penalties
        num_groups = len(rounded_priors)
        pi_pen = jnp.log(jnp.pi ** 2 * num_groups * N_train / (6 * delta))
        log_pen = 0
        for i in range(num_groups):
            curr_lambd = jnp.clip(jnp.exp(rounded_priors[i]), a_max=c)
            log_pen += 2 * jnp.log(b * jnp.log((c+1e-6) / curr_lambd))

        # calculate the KL penalty
        penalty_loss = self.compute_all_params_KL(params[0], params[1],
                                                  rounded_priors) + pi_pen + log_pen
        return penalty_loss / N_train

    def compute_all_params_KL(self, mean_params, sigma_params, lambd):
        # step size
        total_pen = compute_single_param_KL(mean_params[:, 0], jnp.exp(sigma_params[:, 0]),
                                            jnp.exp(lambd[0]))

        # threshold
        total_pen += compute_single_param_KL(mean_params[:, 1], jnp.exp(sigma_params[:, 1]),
                                             jnp.exp(lambd[1]))
        return total_pen

    def compute_weight_norm_squared(self, nn_params):
        return jnp.linalg.norm(nn_params) ** 2, nn_params.size

    def calculate_avg_posterior_var(self, params):
        sigma_params = params[1]
        weight_var = [jnp.ravel(weight_matrix) for weight_matrix, _ in sigma_params]
        bias_var = [jnp.ravel(bias_vector) for _, bias_vector in sigma_params]
        flattened_params = jnp.concatenate(weight_var + bias_var)
        variances = jnp.exp(flattened_params)
        avg_posterior_var = variances.mean()
        stddev_posterior_var = variances.std()
        return avg_posterior_var, stddev_posterior_var
