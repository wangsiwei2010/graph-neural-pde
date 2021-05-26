import torch
from torch import nn
import torch.nn.functional as F
from base_classes import BaseGNN
from model_configurations import set_block, set_function
from graph_rewiring import KNN, add_edges, edge_sampling, GDC
from utils import DummyData, get_full_adjacency

# Define the GNN model.
class GNN_KNN(BaseGNN):
  def __init__(self, opt, dataset, device=torch.device('cpu')):
    super(GNN_KNN, self).__init__(opt, dataset, device)
    self.f = set_function(opt)
    block = set_block(opt)
    time_tensor = torch.tensor([0, self.T]).to(device)
    self.odeblock = block(self.f, self.regularization_fns, opt, dataset.data, device, t=time_tensor).to(device)
    self.data_edge_index = dataset.data.edge_index.to(device)
    self.fa = get_full_adjacency(self.num_nodes).to(device)

  def forward(self, x, pos_encoding):
    # Encode each node based on its feature.
    if self.opt['use_labels']:
      y = x[:, -self.num_classes:]
      x = x[:, :-self.num_classes]

    if self.opt['beltrami']:
      x = F.dropout(x, self.opt['input_dropout'], training=self.training)
      x = self.mx(x)
      if self.opt['dataset'] == 'ogbn-arxiv':
        p = pos_encoding
      else:
        p = F.dropout(pos_encoding, self.opt['input_dropout'], training=self.training)
        p = self.mp(p)
      x = torch.cat([x, p], dim=1)
    else:
      x = F.dropout(x, self.opt['input_dropout'], training=self.training)
      x = self.m1(x)

    if self.opt['use_mlp']:
      x = F.dropout(x, self.opt['dropout'], training=self.training)
      x = F.dropout(x + self.m11(F.relu(x)), self.opt['dropout'], training=self.training)
      x = F.dropout(x + self.m12(F.relu(x)), self.opt['dropout'], training=self.training)

    # todo investigate if some input non-linearity solves the problem with smooth deformations identified in the ANODE paper
    # if True:
    #   x = F.relu(x)
    if self.opt['use_labels']:
      x = torch.cat([x, y], dim=-1)

    if self.opt['batch_norm']:
      x = self.bn_in(x)

    # Solve the initial value problem of the ODE.
    if self.opt['augment']:
      c_aux = torch.zeros(x.shape).to(self.device)
      x = torch.cat([x, c_aux], dim=1)

    # self.odeblock.set_x0(x) #had left in before check if a problem

    # if self.training and self.odeblock.nreg > 0:
    #   z, self.reg_states = self.odeblock(x)
    # else:
    #   z = self.odeblock(x)

    if self.opt['KNN_online']:
      z = x
      for _ in range(self.opt['KNN_online_reps']):
        self.odeblock.set_x0(z)
        if self.training and self.odeblock.nreg > 0:
          z, self.reg_states = self.odeblock(z)
        else:
          z = self.odeblock(z)
        self.odeblock.odefunc.edge_index = KNN(z, self.opt)

    elif self.opt['edge_sampling_online']:
      z = x
      for _ in range(self.opt['edge_sampling_online_reps']):
        self.odeblock.set_x0(z)
        if self.training and self.odeblock.nreg > 0:
          z, self.reg_states = self.odeblock(z)
        else:
          z = self.odeblock(z)
        if self.opt['edge_sampling_add_type'] in ['random','anchored','importance','degree']:
          self.odeblock.odefunc.edge_index = add_edges(self, self.opt)
          self.odeblock.odefunc.edge_index = edge_sampling(self, z, self.opt)
        elif self.opt['edge_sampling_add_type'] == 'gdc':
          diff_args = dict(method='ppr', alpha=self.opt['ppr_alpha'])
          # sparse_args = dict(method='threshold', avg_degree=16)
          sparse_args = dict(method='topk', k=32, dim=0)
          slw = None if self.opt['self_loop_weight']==0 else self.opt['self_loop_weight']
          gdc = GDC(self_loop_weight=slw,
                    normalization_in='sym',
                    normalization_out='col',
                    diffusion_kwargs=diff_args,
                    sparsification_kwargs=sparse_args, exact=True)
          mean_attention = self.odeblock.get_raw_attention_weights(z).mean(dim=1, keepdim=False)
          dummy = DummyData(self.odeblock.odefunc.edge_index, mean_attention, self.num_nodes)
          self.odeblock.odefunc.edge_index = gdc(dummy).edge_index

        elif self.opt['edge_sampling_add_type'] == 'n2_radius': #this is too slow
          self.odeblock.odefunc.edge_index = add_edges(self, self.opt)
          self.odeblock.odefunc.edge_index = edge_sampling(self, z, self.opt)

      self.odeblock.odefunc.edge_index = self.data_edge_index #to reset edge index after diffusion

    else:
      self.odeblock.set_x0(x)
      if self.training and self.odeblock.nreg > 0:
        z, self.reg_states = self.odeblock(x)
      else:
        z = self.odeblock(x)

    if self.opt['fa_layer']:
      temp_time = self.opt['time']
      temp_method = self.opt['method']
      temp_step_size = self.opt['step_size']

      self.opt['time'] = self.opt['fa_layer_time'] #1.0
      self.opt['method'] = self.opt['fa_layer_method']#'rk4'
      self.opt['step_size'] = self.opt['fa_layer_step_size']#1.0
      self.odeblock.set_x0(z)
      self.odeblock.odefunc.edge_index = self.fa
      if self.opt['fa_layer_edge_sampling_rmv'] != 0:
        temp_edge_sampling_rmv = self.opt['edge_sampling_rmv']
        self.opt['edge_sampling_rmv'] = self.opt['fa_layer_edge_sampling_rmv']
        self.odeblock.odefunc.edge_index = edge_sampling(self, z, self.opt)
        self.opt['edge_sampling_rmv'] = temp_edge_sampling_rmv

      z = self.odeblock(z)
      self.odeblock.odefunc.edge_index = self.data_edge_index

      self.opt['time'] = temp_time
      self.opt['method'] = temp_method
      self.opt['step_size'] = temp_step_size


    if self.opt['augment']:
      z = torch.split(z, x.shape[1] // 2, dim=1)[0]

    # if self.opt['batch_norm']:
    #   z = self.bn_in(z)

    # Activation.
    z = F.relu(z)

    if self.opt['fc_out']:
      z = self.fc(z)
      z = F.relu(z)

    # Dropout.
    z = F.dropout(z, self.opt['dropout'], training=self.training)

    # Decode each node embedding to get node label.
    z = self.m2(z)
    return z

  def forward_encoder(self, x, pos_encoding):
    # Encode each node based on its feature.
    if self.opt['use_labels']:
      y = x[:, -self.num_classes:]
      x = x[:, :-self.num_classes]

    if self.opt['beltrami']:
      # x = F.dropout(x, self.opt['input_dropout'], training=self.training)
      x = self.mx(x)
      if self.opt['dataset'] == 'ogbn-arxiv':
        p = pos_encoding
      else:
        # p = F.dropout(pos_encoding, self.opt['input_dropout'], training=self.training)
        p = self.mp(pos_encoding)
      x = torch.cat([x, p], dim=1)
    else:
      # x = F.dropout(x, self.opt['input_dropout'], training=self.training)
      x = self.m1(x)

    if self.opt['use_mlp']:
      # x = F.dropout(x, self.opt['dropout'], training=self.training)
      # x = F.dropout(x + self.m11(F.relu(x)), self.opt['dropout'], training=self.training)
      # x = F.dropout(x + self.m12(F.relu(x)), self.opt['dropout'], training=self.training)
      x = x + self.m11(F.relu(x))
      x = x + self.m12(F.relu(x))

    # todo investigate if some input non-linearity solves the problem with smooth deformations identified in the ANODE paper
    # if True:
    #   x = F.relu(x)
    if self.opt['use_labels']:
      x = torch.cat([x, y], dim=-1)

    if self.opt['batch_norm']:
      x = self.bn_in(x)

    # Solve the initial value problem of the ODE.
    if self.opt['augment']:
      c_aux = torch.zeros(x.shape).to(self.device)
      x = torch.cat([x, c_aux], dim=1)

    return x

  def forward_ODE(self, x, pos_encoding):
    x = self.forward_encoder(x, pos_encoding)

    self.odeblock.set_x0(x)

    if self.training and self.odeblock.nreg > 0:
      z, self.reg_states = self.odeblock(x)
    else:
      z = self.odeblock(x)

    if self.opt['augment']:
      z = torch.split(z, x.shape[1] // 2, dim=1)[0]

    return z