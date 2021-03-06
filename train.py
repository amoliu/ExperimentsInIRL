from sampling_utils import *
from rllab.sampler.base import BaseSampler
from rllab.misc import tensor_utils
from rllab.policies.uniform_control_policy import UniformControlPolicy
from rllab.algos.nop import NOP
from rllab.baselines.zero_baseline import ZeroBaseline
import numpy as np
from scipy.stats import norm
import tensorflow as tf
import gc
import time
from rllab.misc import logger

class Trainer(object):

    def __init__(self, sess, env, cost_approximator, cost_trainer, novice_policy, novice_policy_optimizer, num_frames=4, concat_timesteps=True, train_disc=True):
        """
        sess : tensorflow session
        cost_approximator : the NN or whatever cost function that can take in your observations/states and then give you your reward
        cost_trainer : this is the trainer for optimizing the cost (i.e. runs tensorflow training ops, etc.)
        novice_policy : the policy of your novice agent
        novice_policy_optimizer : the optimizer which runs a policy optimization step (or constrained number of iterations)
        much of this can be found in https://github.com/bstadie/third_person_im/blob/master/sandbox/bradly/third_person/algos/cyberpunk_trainer.py#L164
        """
        self.sess = sess
        self.env = env
        self.cost_approximator = cost_approximator
        self.cost_trainer = cost_trainer
        self.iteration = 0
        self.novice_policy = novice_policy
        self.novice_policy_optimizer = novice_policy_optimizer
        # self.sampler = BaseSampler(self.novice_policy_optimizer)
        self.concat_timesteps = concat_timesteps
        self.num_frames = num_frames
        self.should_train_cost = True
        self.prev_reward_dist = None
        self.is_first_disc_update = True
        self.gc_time = time.time()
        self.gc_time_threshold = 60 # seconds between garbage collection
        # as in traditional GANs, we add failure noise
        self.noise_fail_policy = UniformControlPolicy(env.spec)
        self.train_disc = train_disc
        self.zero_baseline = ZeroBaseline(env_spec=env.spec)
        self.rand_algo = NOP(
            env=env,
            policy=self.noise_fail_policy,
            baseline=self.zero_baseline,
            batch_size=1*self.env.horizon,
            max_path_length=self.env.horizon,
            n_itr=1,
            discount=0.995,
            step_size=0.01,
        )
        self.rand_algo.start_worker() # TODO: Call this in constructor instead ?
        self.rand_algo.init_opt()
        self.replay_pool = RolloutReplayPool(50000,
                                            replacement_policy='stochastic',
                                            replacement_prob=0.95,
                                            max_skip_episode=10)

    def step(self, expert_rollouts_tensor, expert_horizon=200, dump_datapoints=False, number_of_sample_trajectories=None, config={}):
        if number_of_sample_trajectories is None:
            number_of_sample_trajectories = len(expert_rollouts_tensor)


        # This does things like calculate advantages and entropy, etc.
        # if we use the cost function when acquiring the novice rollouts, this will use our cost function
        # for optimizing the trajectories
        orig_novice_rollouts = self.novice_policy_optimizer.obtain_samples(self.iteration)

        if config["use_experience_replay"]:
            for x in orig_novice_rollouts:
                self.replay_pool.add_rollout(x)

        # print("*******************************************")
        logger.record_tabular("TrueReward", np.mean([np.sum(p['rewards']) for p in orig_novice_rollouts]))
        # print("*******************************************")

        novice_rollouts = process_samples_with_reward_extractor(orig_novice_rollouts, self.cost_approximator, self.concat_timesteps, self.num_frames,  batch_size=config["policy_opt_batch_size"])

        # import pdb; pdb.set_trace()

        mu, std = norm.fit(np.concatenate([np.array(p['rewards']).reshape(-1) for p in novice_rollouts]))
        dist = tf.contrib.distributions.Normal(loc=mu, scale=std)

        # if we're using a cost trainer train it?
        if self.cost_trainer and self.should_train_cost and self.train_disc:
            random_rollouts = self.rand_algo.sampler.obtain_samples(itr = self.iteration)

            if config["use_experience_replay"]:
                replay_batch  = self.replay_pool.random_batch(number_of_sample_trajectories)
                train_novice_data = novice_rollouts_tensor = [path["observations"] for path in replay_batch]
            else:
                train_novice_data = novice_rollouts_tensor = [path["observations"] for path in novice_rollouts]
            random_rollouts_tensor = [path["observations"] for path in random_rollouts]
            prev_cost_dist = dist

            # TODO: replace random noise with experience replay
            # Replace first 5 novice rollouts by random policy trajectory
            # if config["algorithm"] != "apprenticeship":# ew hack
            #     # append the two lists
            #     assert type(novice_rollouts_tensor) is list
            #     assert type(random_rollouts_tensor) is list
            #     # train_novice_data = novice_rollouts_tensor + random_rollouts_tensor

            train = True
            cost_t_steps = 0
            while train:
                if self.is_first_disc_update:
                    num_epochs = 3
                    self.is_first_disc_update = False
                else:
                    num_epochs = 1
                ave_loss, ave_acc = self.cost_trainer.train_cost(train_novice_data, expert_rollouts_tensor, number_epochs=num_epochs, num_frames=self.num_frames)
                # import pdb; pdb.set_trace()
                novice_rollouts = process_samples_with_reward_extractor(orig_novice_rollouts, self.cost_approximator, self.concat_timesteps, self.num_frames,  batch_size=config["policy_opt_batch_size"])

                # TODO: this sucks, shouldn't use KL here, but should use KS test? We don't know that the reward distribution comes from a normal
                # so assuming that and doing KL isn't really valid theoretically. KS-test will tell you once they are drawn from different distros
                # If the K-S statistic is small or the p-value is high, then we cannot reject the (null) hypothesis that the distributions of the
                # two samples are the same.
                # Assumes a continuous distribution??
                # scipy.stats.ks_2samp(data1, data2)
                mu, std = norm.fit(np.concatenate([np.array(p['rewards']).reshape(-1) for p in novice_rollouts]))
                dist = tf.contrib.distributions.Normal(loc=mu, scale=std)
                kl_divergence = tf.contrib.distributions.kl_divergence(dist, prev_cost_dist)
                kl = self.sess.run(kl_divergence)
                print("Cost training reward KL divergence: %f" % kl)
                cost_t_steps += 1
                if kl >= .01 or cost_t_steps >= 5:
                    train = False
                # if kl >= .1:
                # Probably need to lower the learning rate so we don't diverge from the distribution so much?


            self.should_train_cost = False
            self.prev_reward_dist = dist


        policy_training_samples = self.novice_policy_optimizer.process_samples(itr=self.iteration, paths=novice_rollouts)

        # import pdb; pdb.set_trace()
        # print("Number of policy opt epochs %d" % policy_opt_epochs)


        # TODO: make this not a tf function, seems like too much overhead
        if self.prev_reward_dist:
            if config["use_kl_learning_for_trpo"]:
            # import pdb; pdb.set_trace()
                kl_divergence = tf.contrib.distributions.kl_divergence(dist, self.prev_reward_dist)
                kl = self.sess.run(kl_divergence)

                logger.record_tabular("RewardDistKLSinceLastCostUpdate",kl)
                kl_with_decay = .1 * (.96 ** (self.iteration/5))
                if kl >= kl_with_decay:
                    self.should_train_cost = True
            else:
                self.should_train_cost = True

        total_len = len(policy_training_samples['observations'])
        for policy_training_sample_batch in batchify_dict(policy_training_samples, batch_size=config["policy_opt_batch_size"], total_len=total_len): #TODO: configurable batch_size
            self.novice_policy_optimizer.optimize_policy(itr=self.iteration, samples_data=policy_training_sample_batch)
            # logger.dump_tabular(with_prefix=False)
        self.iteration += 1

        if dump_datapoints:
            self.cost_trainer.dump_datapoints(self.num_frames)

        if time.time() - self.gc_time > self.gc_time_threshold:
            gc.collect()
            self.gc_time = time.time()
        # print("*******************************************")
        logger.record_tabular("DiscrimReward", np.mean([np.sum(p['rewards']) for p in novice_rollouts]))
        logger.record_tabular("AverageTrajLen",np.mean([len(p['rewards']) for p in novice_rollouts]))
        logger.record_tabular("TrainingIter", self.iteration)
        logger.dump_tabular(with_prefix=False)
        # print("*******************************************")

        return np.mean([np.sum(p['true_rewards']) for p in novice_rollouts]), np.mean([np.sum(p['rewards']) for p in novice_rollouts])
