import os
import statistics
import time
import json
from collections import deque

import numpy as np
import wandb
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.tensorboard import SummaryWriter

from ...runners.modules.discriminator import Discriminator
from ...runners.modules.normalizer import EmpiricalNormalization
from ...runners.modules.policy import Policy
from ...runners.modules.value import Value
from ...runners.storage.rollout_storage import RolloutStorage
from ...runners.storage.replay_buffer import ReplayBuffer
from ...utils.helpers import class_to_dict
from ...utils.wandb_utils import WandbSummaryWriter


class AMP:

    def __init__(self,
                 env,
                 train_cfg,
                 log_dir=None,
                 device='cpu'):

        self.cfg = train_cfg.runner
        self.alg_cfg = train_cfg.algorithm
        self.policy_cfg = train_cfg.policy
        self.alg_name = train_cfg.algorithm_name
        self.device = device
        self.env = env
        self.num_rews = len(self.env.cfg.rewards.group_coeff) + 1  # have another one for style

        # set up actor critic for PPO
        if self.env.num_privileged_obs is not None:
            num_critic_obs = self.env.num_privileged_obs
        else:
            num_critic_obs = self.env.num_obs

        if self.env.include_history_steps is not None:
            num_actor_obs = self.env.num_obs * self.env.include_history_steps
        else:
            num_actor_obs = self.env.num_obs

        self.policy = Policy(num_obs=num_actor_obs,
                             num_actions=self.env.num_actions,
                             hidden_dims=self.policy_cfg.actor_hidden_dims,
                             activation=self.policy_cfg.activation,
                             log_std_init=self.policy_cfg.log_std_init,
                             device=self.device).to(self.device)

        self.value = Value(num_obs=num_critic_obs,
                           hidden_dims=self.policy_cfg.critic_hidden_dims,
                           activation=self.policy_cfg.activation,
                           device=self.device).to(self.device)

        # expert data
        self.amp_data = self.env.amp_loader

        # normalizer
        self.normalize_observation = self.cfg.normalize_observation
        if self.normalize_observation:
            self.actor_obs_normalizer = EmpiricalNormalization(shape=num_actor_obs,
                                                               until=int(1.0e8)).to(self.device)
            self.critic_obs_normalizer = EmpiricalNormalization(shape=num_critic_obs,
                                                                until=int(1.0e8)).to(self.device)
        else:
            self.actor_obs_normalizer = torch.nn.Identity()  # no normalization
            self.critic_obs_normalizer = torch.nn.Identity()  # no normalization

        self.discriminator = Discriminator(input_dim=self.env.num_amp_obs,
                                           hidden_layer_sizes=self.alg_cfg.disc_hidden_dims,
                                           device=device)

        self.normalize_disc_input = self.cfg.normalize_disc_input
        if self.normalize_disc_input:
            self.disc_normalizer = EmpiricalNormalization(shape=self.env.num_amp_obs,
                                                          until=int(1.0e8)).to(self.device)
        else:
            self.disc_normalizer = torch.nn.Identity()  # no normalization

        # storage
        self.storage = RolloutStorage(num_envs=self.env.num_envs,
                                      num_transitions_per_env=self.cfg.num_steps_per_env,
                                      num_obs=num_actor_obs,
                                      num_critic_obs=num_critic_obs,
                                      num_actions=self.env.num_actions,
                                      device=self.device)
        self.transition = RolloutStorage.Transition()

        self.amp_storage = ReplayBuffer(obs_dim=self.discriminator.input_dim,
                                        buffer_size=self.alg_cfg.amp_replay_buffer_size,
                                        device=device)

        # optimizers
        params = list(self.policy.parameters()) + list(self.value.parameters())
        self.optimizer = optim.Adam(params, lr=self.alg_cfg.learning_rate)
        self.learning_rate = self.alg_cfg.learning_rate

        disc_params = [{'params': self.discriminator.trunk.parameters(),
                        'weight_decay': 10e-4, 'name': 'amp_trunk'},
                       {'params': self.discriminator.linear.parameters(),
                        'weight_decay': 10e-2, 'name': 'amp_head'}]
        self.disc_optimizer = optim.Adam(disc_params, lr=self.alg_cfg.disc_learning_rate)

        # Log
        self.log_dir = log_dir
        self.writer = None
        self.init_writer(self.env.cfg.env.play)

        self.tot_timesteps = 0
        self.tot_time = 0
        self.num_iterations = self.cfg.max_iterations
        self.save_interval = self.cfg.save_interval

        self.env.reset()

    def learn(self, ):
        self.env.episode_length_buf = torch.randint_like(self.env.episode_length_buf,
                                                         high=int(self.env.max_episode_length))

        new_obs = self.actor_obs_normalizer(self.env.get_observations())
        new_privileged_obs = self.env.get_privileged_observations()
        if new_privileged_obs is not None:
            new_privileged_obs = self.critic_obs_normalizer(new_privileged_obs)

        self.train_mode()

        ep_infos = []

        task_rew_buffer = deque(maxlen=100)
        imitate_rew_buffer = deque(maxlen=100)
        rew_buffer = [deque(maxlen=100) for _ in range(self.num_rews)]
        len_buffer = deque(maxlen=100)
        cur_task_reward_sum = torch.zeros(self.env.num_envs, dtype=torch.float, device=self.device)
        cur_imitate_reward_sum = torch.zeros(self.env.num_envs, dtype=torch.float, device=self.device)
        cur_reward_sum = [torch.zeros(self.env.num_envs, dtype=torch.float, device=self.device) for _ in
                          range(self.num_rews)]
        cur_episode_length = torch.zeros(self.env.num_envs, dtype=torch.float, device=self.device)

        filming = False
        filming_imgs = []
        filming_iter_counter = 0

        for it in range(self.num_iterations):
            self.it = it
            start = time.time()

            # filming
            if self.cfg.record_gif and (it % self.cfg.record_gif_interval == 0):
                filming = True

            # Rollout
            with torch.inference_mode():
                for i in range(self.cfg.num_steps_per_env):
                    obs = new_obs  # normalized already
                    privileged_obs = new_privileged_obs  # normalized already
                    actions, log_prob = self.policy.act_and_log_prob(obs)
                    new_obs, new_privileged_obs, amp_obs, group_rew, dones, infos = self.env.step(actions)
                    critic_obs = privileged_obs if privileged_obs is not None else obs

                    rews = [group_rew[:, i] for i in range(self.num_rews - 1)]
                    imitate_d = self.discriminator(self.disc_normalizer(amp_obs)).squeeze()
                    style_rew = torch.clamp(1 - (1 / 4) * torch.square(imitate_d - 1), min=0)
                    rews.append(style_rew)

                    # obs, critic_obs are normalized, amp_obs is not
                    self.process_env_step(
                        obs, actions, log_prob, critic_obs, rews, dones, infos, amp_obs, self.alg_cfg.bootstrap)

                    new_obs = self.actor_obs_normalizer(new_obs)
                    if new_privileged_obs is not None:
                        new_privileged_obs = self.critic_obs_normalizer(new_privileged_obs)

                    if filming:
                        filming_imgs.append(self.env.camera_image)

                    if self.log_dir is not None:
                        if 'episode' in infos:
                            ep_infos.append(infos['episode'])

                        new_ids = (dones > 0).nonzero(as_tuple=False)

                        for j in range(self.num_rews):
                            cur_reward_sum[j] += rews[j]
                            rew_buffer[j].extend(
                                cur_reward_sum[j][new_ids][:, 0].cpu().numpy().tolist())
                            cur_reward_sum[j][new_ids] = 0

                        cur_episode_length += 1
                        len_buffer.extend(cur_episode_length[new_ids][:, 0].cpu().numpy().tolist())
                        cur_episode_length[new_ids] = 0

                last_critic_obs = critic_obs.detach()
                last_values = self.value(last_critic_obs).detach()
                self.storage.compute_returns(last_values, self.alg_cfg.gamma, self.alg_cfg.lam)

            stop = time.time()
            collection_time = stop - start

            start = stop
            mean_value_loss, mean_surrogate_loss, mean_disc_loss, mean_grad_pen_loss, mean_policy_pred, \
                mean_expert_pred = self.update()
            stop = time.time()
            learn_time = stop - start

            if filming:
                filming_iter_counter += 1
                if filming_iter_counter == self.cfg.record_iters:
                    export_imgs = np.array(filming_imgs)
                    if self.cfg.wandb:
                        fps = 1. / self.env.dt
                        wandb.log({'Video': wandb.Video(export_imgs.transpose(0, 3, 1, 2), fps=fps, format="mp4")})
                    del export_imgs
                    filming = False
                    filming_imgs = []
                    filming_iter_counter = 0

            if self.log_dir is not None:
                self.log(locals())

            if it % self.save_interval == 0:
                self.save(os.path.join(self.log_dir, 'model_{}.pt'.format(it)))

            ep_infos.clear()

        self.save(os.path.join(self.log_dir, 'model_{}.pt'.format(self.num_iterations)))

    def process_env_step(self,
                         obs,
                         actions,
                         log_prob,
                         critic_obs,
                         rewards,
                         dones,
                         infos,
                         new_amp_obs,
                         bootstrap=True):
        self.transition.observations = obs.detach()
        self.transition.critic_observations = critic_obs.detach()

        self.transition.actions = actions.detach()
        self.transition.actions_log_prob = log_prob.detach()
        self.transition.action_mean = self.policy.action_mean.detach()
        self.transition.action_sigma = self.policy.action_std.detach()

        # curriculum for rew coefficients
        curriculum_ratio = np.clip((self.it - float(self.env.cfg.rewards.curr_start_iter)) /
                                   (self.env.cfg.rewards.curr_end_iter - self.env.cfg.rewards.curr_start_iter),
                                   a_min=0.0, a_max=1.0)
        coefs = [0] * len(self.env.cfg.rewards.group_coeff)
        for name, coef in self.env.cfg.rewards.group_coeff.items():
            i = self.env.rew_group_ids[name]
            coefs[i] = coef
            if self.env.cfg.rewards.group_coeff_curriculum[name]:
                coefs[i] = coef + curriculum_ratio * self.env.cfg.rewards.change_value[name]
        coefs.append(self.alg_cfg.amp_coef)

        if self.alg_cfg.normalize_coeffs:
            norm_coefs = [c / sum(coefs) for c in coefs]  # normalize coefficients
            coefs = norm_coefs

        self.transition.rewards_raw = [rewards[i].clone() for i in range(self.num_rews)]
        self.transition.rewards = sum([coefs[i] * rewards[i].clone() for i in range(self.num_rews)])
        self.transition.dones = dones
        self.transition.values = self.value(critic_obs).detach()

        if bootstrap:
            self.transition.rewards += self.alg_cfg.gamma * torch.squeeze(
                self.transition.values * infos['time_outs'].unsqueeze(1).to(self.device), 1)

        self.storage.add_transitions(self.transition)
        self.transition.clear()
        self.amp_storage.insert(new_amp_obs)

    def update(self):
        mean_value_loss = 0
        mean_surrogate_loss = 0
        mean_disc_loss = 0
        mean_grad_pen_loss = 0
        mean_policy_pred = 0
        mean_expert_pred = 0

        generator = self.storage.mini_batch_generator(self.alg_cfg.num_mini_batches,
                                                      self.alg_cfg.num_learning_epochs)
        num_updates = self.alg_cfg.num_learning_epochs * self.alg_cfg.num_mini_batches
        for sample in generator:
            obs, critic_obs, actions, target_values, advantages, returns, old_actions_log_prob, \
                old_mu, old_sigma = sample

            # update action distribution with sampled obs
            _, _ = self.policy.act_and_log_prob(obs)
            actions_log_prob = self.policy.distribution.log_prob(actions)
            value = self.value(critic_obs)

            mu = self.policy.action_mean
            sigma = self.policy.action_std
            entropy = self.policy.entropy

            # adaptively change the lr for PPO using KL divergence
            if self.alg_cfg.desired_kl is not None and self.alg_cfg.schedule == 'adaptive':
                with torch.inference_mode():
                    kl = torch.sum(
                        torch.log(sigma / old_sigma + 1.e-5) + (
                                torch.square(old_sigma) + torch.square(old_mu - mu)) / (
                                2.0 * torch.square(sigma)) - 0.5, dim=-1)
                    kl_mean = torch.mean(kl)

                    if kl_mean > self.alg_cfg.desired_kl * 2.0:
                        self.learning_rate = max(1e-5, self.learning_rate / 1.5)
                    elif self.alg_cfg.desired_kl / 2.0 > kl_mean > 0.0:
                        self.learning_rate = min(1e-2, self.learning_rate * 1.5)

                    for param_group in self.optimizer.param_groups:
                        param_group['lr'] = self.learning_rate

            # Surrogate loss
            ratio = torch.exp(actions_log_prob - torch.squeeze(old_actions_log_prob))
            surrogate = -torch.squeeze(advantages) * ratio
            surrogate_clipped = -torch.squeeze(advantages) * torch.clamp(ratio, 1.0 - self.alg_cfg.clip_param,
                                                                         1.0 + self.alg_cfg.clip_param)
            surrogate_loss = torch.max(surrogate, surrogate_clipped).mean()

            # Value function loss
            if self.alg_cfg.use_clipped_value_loss:
                value_clipped = target_values + (value - target_values).clamp(
                    -self.alg_cfg.clip_param,
                    self.alg_cfg.clip_param)
                value_losses = (value - returns).pow(2)
                value_losses_clipped = (value_clipped - returns).pow(2)
                value_loss = torch.max(value_losses, value_losses_clipped).mean()
            else:
                value_loss = (returns - value).pow(2).mean()

            # Compute total loss.
            loss = self.alg_cfg.surrogate_coef * surrogate_loss + self.alg_cfg.value_loss_coef * value_loss - self.alg_cfg.entropy_coef * entropy.mean()

            # Gradient step
            self.optimizer.zero_grad()
            loss.backward()
            params_list = list(self.policy.parameters()) + list(self.value.parameters())
            nn.utils.clip_grad_norm_(params_list, self.alg_cfg.max_grad_norm)
            self.optimizer.step()

            mean_value_loss += value_loss.item()
            mean_surrogate_loss += surrogate_loss.item()

        mean_value_loss /= num_updates
        mean_surrogate_loss /= num_updates

        amp_policy_generator = self.amp_storage.feed_forward_generator(
            self.alg_cfg.num_learning_epochs * self.alg_cfg.num_mini_batches,
            self.storage.num_envs * self.storage.num_transitions_per_env //
            self.alg_cfg.num_mini_batches)

        amp_expert_generator = self.amp_data.feed_forward_generator(
            self.alg_cfg.num_learning_epochs * self.alg_cfg.num_mini_batches,
            self.storage.num_envs * self.storage.num_transitions_per_env //
            self.alg_cfg.num_mini_batches)

        num_disc_updates = self.alg_cfg.num_learning_epochs * self.alg_cfg.num_mini_batches

        for sample_amp_policy, sample_amp_expert in zip(amp_policy_generator, amp_expert_generator):
            self.disc_normalizer.eval()
            policy_state = self.disc_normalizer(sample_amp_policy)
            expert_state = self.disc_normalizer(sample_amp_expert)
            self.disc_normalizer.train()

            policy_d = self.discriminator(policy_state)
            expert_d = self.discriminator(expert_state)

            if self.alg_cfg.disc_loss == "LSGAN":
                expert_loss = torch.nn.MSELoss()(expert_d, torch.ones(expert_d.size(), device=self.device))
                policy_loss = torch.nn.MSELoss()(policy_d, -1 * torch.ones(policy_d.size(), device=self.device))
                disc_loss = 0.5 * (expert_loss + policy_loss)
            elif self.alg_cfg.disc_loss == "WGAN":
                disc_loss = policy_d.mean() - expert_d.mean()
            else:
                raise NotImplementedError

            grad_pen_loss = self.discriminator.compute_grad_pen(expert_state)

            amp_loss = disc_loss + grad_pen_loss
            #
            # # sync lr
            # for param_group in self.disc_optimizer.param_groups:
            #     param_group['lr'] = self.learning_rate

            self.disc_optimizer.zero_grad()
            amp_loss.backward()
            self.disc_optimizer.step()

            mean_disc_loss += amp_loss.item()
            mean_grad_pen_loss += grad_pen_loss.item()
            mean_policy_pred += policy_d.mean().item()
            mean_expert_pred += expert_d.mean().item()

        mean_disc_loss /= num_disc_updates
        mean_grad_pen_loss /= num_disc_updates
        mean_policy_pred /= num_disc_updates
        mean_expert_pred /= num_disc_updates
        self.storage.clear()

        return mean_value_loss, mean_surrogate_loss, mean_disc_loss, mean_grad_pen_loss, mean_policy_pred, \
            mean_expert_pred

    def log(self, locs, width=80, pad=35):
        self.tot_timesteps += self.cfg.num_steps_per_env * self.env.num_envs
        self.tot_time += locs['collection_time'] + locs['learn_time']
        iteration_time = locs['collection_time'] + locs['learn_time']

        ep_string = f''
        if locs['ep_infos']:
            for key in locs['ep_infos'][0]:
                infotensor = torch.tensor([], device=self.device)
                for ep_info in locs['ep_infos']:
                    # handle scalar and zero dimensional tensor infos
                    if not isinstance(ep_info[key], torch.Tensor):
                        ep_info[key] = torch.Tensor([ep_info[key]])
                    if len(ep_info[key].shape) == 0:
                        ep_info[key] = ep_info[key].unsqueeze(0)
                    infotensor = torch.cat((infotensor, ep_info[key].to(self.device)))
                value = torch.mean(infotensor)
                self.writer.add_scalar('Episode/' + key, value, locs['it'])
                ep_string += f"""{f'Mean episode {key}:':>{pad}} {value:.4f}\n"""
        mean_std = self.policy.action_std.mean()
        fps = int(self.cfg.num_steps_per_env * self.env.num_envs / (locs['collection_time'] + locs['learn_time']))

        self.writer.add_scalar('Learn/value_function_loss', locs['mean_value_loss'], locs['it'])
        self.writer.add_scalar('Learn/surrogate_loss', locs['mean_surrogate_loss'], locs['it'])
        self.writer.add_scalar('Learn/amp_disc_loss', locs['mean_disc_loss'], locs['it'])
        self.writer.add_scalar('Learn/amp_grad_pen_loss', locs['mean_grad_pen_loss'], locs['it'])
        self.writer.add_scalar('Learn/learning_rate', self.learning_rate, locs['it'])
        self.writer.add_scalar('Learn/mean_noise_std', mean_std.item(), locs['it'])
        self.writer.add_scalar('Learn/mean_policy_pred', locs['mean_policy_pred'], locs['it'])
        self.writer.add_scalar('Learn/mean_expert_pred', locs['mean_expert_pred'], locs['it'])
        self.writer.add_scalar('Perf/total_fps', fps, locs['it'])
        self.writer.add_scalar('Perf/collection time', locs['collection_time'], locs['it'])
        self.writer.add_scalar('Perf/learning_time', locs['learn_time'], locs['it'])
        if len(locs['rew_buffer']) > 0:
            for name in self.env.reward_groups.keys():
                self.writer.add_scalar('Train/mean_task_reward_{}'.format(name),
                                       statistics.mean(locs['rew_buffer'][self.env.rew_group_ids[name]]), locs['it'])
            self.writer.add_scalar('Train/mean_amp_reward',
                                   statistics.mean(locs['rew_buffer'][-1]), locs['it'])
            self.writer.add_scalar('Train/mean_episode_length', statistics.mean(locs['len_buffer']), locs['it'])

        if self.cfg.wandb:
            self.writer.flush_logger(locs['it'])

        str = f" \033[1m Learning iteration {locs['it']}/{self.num_iterations} \033[0m "

        if len(locs['rew_buffer']) > 0:
            log_string = (f"""{'#' * width}\n"""
                          f"""{str.center(width, ' ')}\n\n"""
                          f"""{'Computation:':>{pad}} {fps:.0f} steps/s (collection: {locs[
                              'collection_time']:.3f}s, learning {locs['learn_time']:.3f}s)\n"""
                          f"""{'Value function loss:':>{pad}} {locs['mean_value_loss']:.4f}\n"""
                          f"""{'Surrogate loss:':>{pad}} {locs['mean_surrogate_loss']:.4f}\n"""
                          f"""{'AMP discriminator loss:':>{pad}} {locs['mean_disc_loss']:.4f}\n"""
                          f"""{'AMP grad pen loss:':>{pad}} {locs['mean_grad_pen_loss']:.4f}\n"""
                          f"""{'AMP mean policy pred:':>{pad}} {locs['mean_policy_pred']:.4f}\n"""
                          f"""{'AMP mean expert pred:':>{pad}} {locs['mean_expert_pred']:.4f}\n"""
                          f"""{'Mean action noise std:':>{pad}} {mean_std.item():.2f}\n"""
                          f"""{'Mean episode length:':>{pad}} {statistics.mean(locs['len_buffer']):.2f}\n""")
        else:
            log_string = (f"""{'#' * width}\n"""
                          f"""{str.center(width, ' ')}\n\n"""
                          f"""{'Computation:':>{pad}} {fps:.0f} steps/s (collection: {locs[
                              'collection_time']:.3f}s, learning {locs['learn_time']:.3f}s)\n"""
                          f"""{'Value function loss:':>{pad}} {locs['mean_value_loss']:.4f}\n"""
                          f"""{'Surrogate loss:':>{pad}} {locs['mean_surrogate_loss']:.4f}\n"""
                          f"""{'Mean action noise std:':>{pad}} {mean_std.item():.2f}\n""")

        log_string += ep_string
        log_string += (f"""{'-' * width}\n"""
                       f"""{'Total timesteps:':>{pad}} {self.tot_timesteps}\n"""
                       f"""{'Iteration time:':>{pad}} {iteration_time:.2f}s\n"""
                       f"""{'Total time:':>{pad}} {self.tot_time:.2f}s\n"""
                       f"""{'ETA:':>{pad}} {self.tot_time / (locs['it'] + 1) * (
                               self.num_iterations - locs['it']):.1f}s\n""")
        print(log_string)

    def save(self, path, infos=None):
        save_dict = {
            'policy_dict': self.policy.state_dict(),
            'value_dict': self.value.state_dict(),
            'optimizer_state_dict': self.optimizer.state_dict(),
            'disc_optimizer_state_dict': self.optimizer.state_dict(),
            'discriminator_state_dict': self.discriminator.state_dict(),
            'infos': infos,
        }
        if self.normalize_observation:
            save_dict["actor_obs_normalizer"] = self.actor_obs_normalizer.state_dict()
            save_dict["critic_obs_normalizer"] = self.critic_obs_normalizer.state_dict()
        if self.normalize_disc_input:
            save_dict["disc_normalizer"] = self.disc_normalizer.state_dict()
        torch.save(save_dict, path)
        if self.cfg.wandb:
            # upload latest model
            new_path = os.path.join(os.path.dirname(path), 'model.pt')
            torch.save(save_dict, new_path)
            wandb.save(new_path, policy="live")

    def load(self, path, load_optimizer=False, load_normalizers=True):
        loaded_dict = torch.load(path)
        self.policy.load_state_dict(loaded_dict['policy_dict'])
        self.value.load_state_dict(loaded_dict['value_dict'])
        self.discriminator.load_state_dict(loaded_dict['discriminator_state_dict'])
        if load_optimizer:
            self.optimizer.load_state_dict(loaded_dict['optimizer_state_dict'])
            self.disc_normalizer.load_state_dict(loaded_dict['disc_optimizer_state_dict'])
        if load_normalizers:
            self.actor_obs_normalizer.load_state_dict(loaded_dict['actor_obs_normalizer'])
            self.critic_obs_normalizer.load_state_dict(loaded_dict['critic_obs_normalizer'])
            self.disc_normalizer.load_state_dict(loaded_dict['disc_normalizer'])
        return loaded_dict['infos']

    def get_inference_policy(self, device=None):
        self.eval_mode()  # switch to evaluation mode (dropout for example)
        if device is not None:
            self.policy.to(device)
            self.actor_obs_normalizer.to(device)

        def inference_policy(x):
            return self.policy.act_inference(self.actor_obs_normalizer(x))

        return inference_policy

    def get_inference_disc(self, device=None):
        self.eval_mode()
        if device is not None:
            self.discriminator.to(device)
            self.disc_normalizer.to(device)

        def inference_disc(x):
            return self.discriminator(self.disc_normalizer(x))

        return inference_disc

    def train_mode(self):
        self.policy.train()
        self.value.train()
        self.discriminator.train()
        if self.normalize_observation:
            self.actor_obs_normalizer.train()
            self.critic_obs_normalizer.train()
        if self.normalize_disc_input:
            self.disc_normalizer.train()

    def eval_mode(self):
        self.policy.eval()
        self.value.eval()
        self.discriminator.eval()
        if self.normalize_observation:
            self.actor_obs_normalizer.eval()
            self.critic_obs_normalizer.eval()
        if self.normalize_disc_input:
            self.disc_normalizer.eval()

    def init_writer(self, play):
        if play:
            return
        # initialize writer
        if self.cfg.wandb:
            self.writer = WandbSummaryWriter(log_dir=self.log_dir, flush_secs=10, cfg=self.cfg,
                                             group=self.cfg.wandb_group)
            self.writer.log_config(self.env.cfg, self.cfg, self.alg_cfg, self.policy_cfg, self.alg_name)
        else:
            self.writer = SummaryWriter(log_dir=self.log_dir, flush_secs=10)

        print(json.dumps(class_to_dict(self.env.cfg), indent=2, default=str))
        print(json.dumps(class_to_dict(self.cfg), indent=2, default=str))
        print(json.dumps(class_to_dict(self.alg_cfg), indent=2, default=str))
        print(json.dumps(class_to_dict(self.policy_cfg), indent=2, default=str))

    def close(self):
        self.env.gym.destroy_sim(self.env.sim)
        self.env.gym.destroy_viewer(self.env.viewer)