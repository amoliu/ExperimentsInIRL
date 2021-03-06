from rllab.baselines.linear_feature_baseline import LinearFeatureBaseline
from rllab.envs.gym_env import GymEnv
from rllab.envs.normalized_env import normalize
import os.path as osp
from rllab.baselines.zero_baseline import ZeroBaseline
from baselines.gaussian_mlp_baseline import *

from rllab.misc.instrument import stub, run_experiment_lite

from sandbox.rocky.tf.envs.base import TfEnv
from sandbox.rocky.tf.policies.categorical_mlp_policy import CategoricalMLPPolicy
from sandbox.rocky.tf.policies.categorical_conv_policy import CategoricalConvPolicy
from sandbox.rocky.tf.policies.gaussian_mlp_policy import GaussianMLPPolicy
from sandbox.rocky.tf.algos.trpo import TRPO
from sandbox.rocky.tf.optimizers.conjugate_gradient_optimizer import ConjugateGradientOptimizer
from sandbox.rocky.tf.optimizers.conjugate_gradient_optimizer import FiniteDifferenceHvp
from sandbox.rocky.tf.spaces.discrete import Discrete

from sampling_utils import sample_policy_trajectories
import pickle
import tensorflow as tf
from envs.transfer.register_envs import register_custom_envs
import numpy as np
import argparse
from policies.categorical_decomposed_policy import CategoricalDecomposedPolicy
from policies.gaussian_decomposed_policy import GaussianDecomposedPolicy
import roboschool

from rllab.misc import ext
from envs.doom_env import DoomEnv


# stub(globals())
ext.set_seed(1)
register_custom_envs()

parser = argparse.ArgumentParser()
parser.add_argument("env")
# parser.add_argument("expert_rollout_pickle_path")
parser.add_argument("num_iters", type=int)
parser.add_argument("--run_baseline", action="store_true")
parser.add_argument("--use_ec2", action="store_true")
parser.add_argument("--data_dir", default="./data")
parser.add_argument("--dont_terminate_machine", action="store_false", help="Whether to terminate your spot instance or not. Be careful.")
parser.add_argument("--batch_size", type=int, default=5000)
args = parser.parse_args()

# stub(globals())
#
# supported_envs = ["MountainCar-v0", "CartPole-v0"]
#
# if args.env not in supported_envs:
#     raise Exception("Env not supported! Try it out though?")

# Need to wrap in a tf environment and force_reset to true
# see https://github.com/openai/rllab/issues/87#issuecomment-282519288


if 'Doom' in args.env:
    gymenv = DoomEnv(args.env, force_reset=True, record_video=False, record_log=False)
    # import pdb; pdb.set_trace()
    orig_obs_dim = gymenv.spec.observation_space.low.shape
    from envs.observation_transform_wrapper import ObservationTransformWrapper
    from envs.transformers import ResizeImageTransformer, SimpleNormalizePixelIntensitiesTransformer, RandomSensorMaskTransformer
    transformers = [SimpleNormalizePixelIntensitiesTransformer()]#, ResizeImageTransformer(fraction_of_current_size=.35)]
    transformed_env = ObservationTransformWrapper(gymenv, transformers)
else:
    gymenv = GymEnv(args.env, force_reset=True, record_video=True, record_log=True)

# gymenv.env.seed(124)
env = TfEnv(normalize(gymenv, normalize_obs=False))

if args.run_baseline:
    if type(env.spec.action_space) is Discrete:
        if 'Doom' in args.env:
            policy = CategoricalConvPolicy(
                name="policy",
                env_spec=env.spec,
                conv_filters=[16, 16],
                conv_filter_sizes=[3, 3],
                conv_strides=[2, 2],
                conv_pads=['VALID', 'VALID'],
                # The neural network policy should have two hidden layers, each with 100 hidden units each (see RLGAN paper)
                hidden_sizes=[200, 200]
            )
        else:
            policy = CategoricalMLPPolicy(
            name="policy",
            env_spec=env.spec,
            # The neural network policy should have two hidden layers, each with 32 hidden units.
            hidden_sizes=(32, 32),
            )
    else:
        policy = GaussianMLPPolicy(
        name="policy",
        env_spec=env.spec,
        # The neural network policy should have two hidden layers, each with 32 hidden units.
        hidden_sizes=(100, 50, 25),
        hidden_nonlinearity=tf.nn.relu,
        )
else:
    if type(env.spec.action_space) is Discrete:
        if 'Doom' in args.env:
            policy = CategoricalDecomposedPolicy(
            name="policy",
            env_spec=env.spec,
            # The neural network policy should have two hidden layers, each with 32 hidden units.
            hidden_sizes=(8, 8),
            conv_filters = [16, 16], conv_filter_sizes = [4, 4], conv_strides = [2,2], conv_pads = ['VALID', 'VALID'], input_shape=orig_obs_dim
            )
        else:
            policy = CategoricalDecomposedPolicy(
            name="policy",
            env_spec=env.spec,
            # The neural network policy should have two hidden layers, each with 32 hidden units.
            hidden_sizes=(8, 8),
            )
    else:
        policy = GaussianDecomposedPolicy(
        name="policy",
        env_spec=env.spec,
        hidden_sizes=(50, 25, 10),
        hidden_nonlinearity=tf.nn.relu,
        num_options = 4
        )

if "Doom" in args.env:
    baseline = GaussianMLPBaseline(env_spec=env.spec)
else:
    baseline = LinearFeatureBaseline(env_spec=env.spec)


iters = args.num_iters

algo = TRPO(
    env=env,
    policy=policy,
    baseline=baseline,
    batch_size=args.batch_size, # Mujoco tasks need 20000-50000
    max_path_length=env.horizon, # And 500
    n_itr=iters,
    discount=0.99,
    step_size=0.01,
    optimizer=ConjugateGradientOptimizer(reg_coef=0.1, hvp_approach=FiniteDifferenceHvp(base_eps=1e-5))
)

run_experiment_lite(
    algo.train(),
    log_dir=None if args.use_ec2 else args.data_dir,
    # Number of parallel workers for sampling
    n_parallel=1,
    # Only keep the snapshot parameters for the last iteration
    snapshot_mode="last",
    # Specifies the seed for the experiment. If this is not provided, a random seed
    # will be used
    exp_prefix="LifeLongLearning_" + args.env + "_trpo",
    seed=1,
    mode="ec2" if args.use_ec2 else "local",
    plot=False,
    # dry=True,
    terminate_machine=args.dont_terminate_machine,
    added_project_directories=[osp.abspath(osp.join(osp.dirname(__file__), '.'))]
)
