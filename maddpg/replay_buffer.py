import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
import random
from collections import deque
import utils

from segment_tree import SumSegmentTree, MinSegmentTree


class ReplayBuffer(object):
    def __init__(self, obs_shape, capacity, device):
        self.capacity = capacity
        self.device = device

        self.obses = np.empty((capacity, obs_shape), dtype=np.float32)
        self.next_obses = np.empty((capacity, obs_shape), dtype=np.float32)
        self.actions = np.empty((capacity, 3), dtype=np.float32)
        self.rewards = np.empty((capacity, 1), dtype=np.float32)
        self.not_dones = np.empty((capacity, 1), dtype=np.float32)

        self.idx = 0
        self.full = False

    def __len__(self):
        return self.capacity if self.full else self.idx

    def add(self, obs, action, reward, next_obs, done):
        np.copyto(self.obses[self.idx], obs)
        np.copyto(self.actions[self.idx], action)
        np.copyto(self.rewards[self.idx], reward)
        np.copyto(self.next_obses[self.idx], next_obs)
        np.copyto(self.not_dones[self.idx], not done)

        self.idx = (self.idx + 1) % self.capacity
        self.full = self.full or self.idx == 0

    def fetch(self, idxs, discount, n):
        assert idxs.max() + n <= len(self)

        obses = self.obses[idxs]
        next_obses = self.next_obses[idxs + n - 1]

        obses = torch.as_tensor(obses, device=self.device).float()
        next_obses = torch.as_tensor(next_obses, device=self.device).float()

        actions = torch.as_tensor(self.actions[idxs], device=self.device)
        rewards = np.zeros((idxs.shape[0], 1), dtype=np.float32)
        not_dones = np.ones((idxs.shape[0], 1), dtype=np.float32)
        for i in range(n):
            rewards += (discount**i) * not_dones * np.sign(
                self.rewards[idxs + i])
            not_dones = np.minimum(not_dones, self.not_dones[idxs + i])

        rewards = torch.as_tensor(rewards, device=self.device)
        not_dones = torch.as_tensor(not_dones, device=self.device)

        return obses, actions, rewards, next_obses, not_dones

    def sample_idxs(self, batch_size, n):
        last_idx = (self.capacity if self.full else self.idx) - (n - 1)
        idxs = np.random.randint(0, last_idx, size=batch_size)

        return idxs

    def sample_multistep(self, batch_size, discount, n):
        assert n <= self.idx or self.full
        idxs = self.sample_idxs(batch_size, n)
        return self.fetch(idxs, discount, n)


class PrioritizedReplayBuffer(ReplayBuffer):
    def __init__(self, obs_shape, capacity, alpha, device):
        super().__init__(obs_shape, capacity, device)

        assert alpha >= 0
        self.alpha = alpha

        tree_capacity = 1
        while tree_capacity < capacity:
            tree_capacity *= 2

        self.sum_tree = SumSegmentTree(tree_capacity)
        self.min_tree = MinSegmentTree(tree_capacity)
        self.max_priority = 1.0

    def add(self, obs, action, reward, next_obs, done):
        super().add(obs, action, reward, next_obs, done)
        self.sum_tree[self.idx] = self.max_priority**self.alpha
        self.min_tree[self.idx] = self.max_priority**self.alpha

    def sample_idxs(self, batch_size, n):
        idxs = []
        p_total = self.sum_tree.sum(0, len(self) - n - 1)
        every_range_len = p_total / batch_size
        for i in range(batch_size):
            while True:
                mass = np.random.rand() * every_range_len + i * every_range_len
                idx = self.sum_tree.find_prefixsum_idx(mass)
                if idx + n <= len(self):
                    idxs.append(idx)
                    break
        return np.array(idxs)

    def sample_multistep(self, batch_size, beta, discount, n):
        assert n <= self.idx or self.full
        assert beta > 0

        idxs = self.sample_idxs(batch_size, n)

        weights = []
        p_min = self.min_tree.min() / self.sum_tree.sum()
        max_weight = (p_min * len(self))**(-beta)

        for idx in idxs:
            p_sample = self.sum_tree[idx] / self.sum_tree.sum()
            weight = (p_sample * len(self))**(-beta)
            weights.append(weight / max_weight)
        weights = torch.as_tensor(np.array(weights),
                                  device=self.device).unsqueeze(dim=1)

        sample = self.fetch(idxs, discount, n)

        return tuple(list(sample) + [weights, idxs])

    def update_priorities(self, idxs, prios):
        assert idxs.shape[0] == prios.shape[0]

        for idx, prio in zip(idxs, prios):
            assert prio > 0
            assert 0 <= idx < len(self)

            self.sum_tree[idx] = prio**self.alpha
            self.min_tree[idx] = prio**self.alpha

            self.max_priority = max(self.max_priority, prio)

class MultiAgentReplayBuffer:
    
    def __init__(self, num_agents, max_size):
        self.max_size = max_size
        self.num_agents = num_agents
        self.buffer = deque(maxlen=max_size)
    
    def push(self, state, action, reward, next_state, done):
        experience = (state, action, np.array(reward), next_state, done)
        self.buffer.append(experience)
    
    def sample(self, batch_size):
        obs_batch = [[] for _ in range(self.num_agents)]  # [ [states of agent 1], ... ,[states of agent n] ]    ]
        indiv_action_batch = [[] for _ in range(self.num_agents)] # [ [actions of agent 1], ... , [actions of agent n]]
        indiv_reward_batch = [[] for _ in range(self.num_agents)]
        next_obs_batch = [[] for _ in range(self.num_agents)]

        global_state_batch = []
        global_next_state_batch = []
        global_actions_batch = []
        done_batch = []

        batch = random.sample(self.buffer, batch_size)


        for experience in batch:
            state, action, reward, next_state, done = experience
            
            for i in range(self.num_agents):
                obs_i = state[i]
                
                action_i = action[i]
                reward_i = reward[i]
                next_obs_i = next_state[i]
            
                obs_batch[i].append(obs_i)
                indiv_action_batch[i].append(action_i)
                indiv_reward_batch[i].append(reward_i)
                next_obs_batch[i].append(next_obs_i)

            global_state_batch.append(np.concatenate(state))
            global_actions_batch.append(torch.cat(action))
            global_next_state_batch.append(np.concatenate(next_state))
            done_batch.append(done)
        return obs_batch, indiv_action_batch, indiv_reward_batch, next_obs_batch, global_state_batch, global_actions_batch, global_next_state_batch, done_batch

    def __len__(self):
        return len(self.buffer)