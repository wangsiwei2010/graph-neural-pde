"""
functions to generate a graph from the input graph and features
"""
import argparse
import time
import numpy as np
import pandas as pd
import torch
from torch_geometric.transforms.two_hop import TwoHop
from torch_geometric.utils import add_self_loops, is_undirected, to_dense_adj, remove_self_loops, contains_self_loops, homophily_ratio
# from torch_geometric.transforms import GDC
from utils import get_rw_adj
from data import get_dataset
from graph_rewiring import get_two_hop, apply_gdc, GDC, dirichlet_energy
from run_GNN import print_model_params, get_optimizer, test_OGB, test, train
from GNN import GNN
from GNN_GCN import GCN
from DIGL_data import PPRDataset, set_train_val_test_split #HeatDataset


def rewiring_get_optimizer(name, model, lr, weight_decay):
  return torch.optim.Adam(
    [
      {'params': model.non_reg_params, 'weight_decay': 0},
      {'params': model.reg_params, 'weight_decay': weight_decay}
    ],
    lr=lr)

#test_OGB, test
def rewiring_train(model, optimizer, data):
  model.train()
  optimizer.zero_grad()
  feat = data.x
  if model.opt['use_labels']:
    train_label_idx, train_pred_idx = get_label_masks(data, model.opt['label_rate'])
    feat = add_labels(feat, data.y, train_label_idx, model.num_classes, model.device)
  else:
    # todo in the DGL code from OGB, they seem to apply an additional mask to the training data - currently commented
    # mask_rate = 0.5
    # mask = torch.rand(data.train_index.shape) < mask_rate
    # train_pred_idx = data.train_idx[mask]
    train_pred_idx = data.train_mask

  out = model(feat)
  if model.opt['dataset'] == 'ogbn-arxiv':
    lf = torch.nn.functional.nll_loss
    loss = lf(out.log_softmax(dim=-1)[data.train_mask], data.y.squeeze(1)[data.train_mask])
  else:
    # nll used in DIGL example
    loss = torch.nn.functional.nll_loss(out[data.train_mask], data.y.squeeze()[data.train_mask])
    # lf = torch.nn.CrossEntropyLoss()
    # loss = lf(out[data.train_mask], data.y.squeeze()[data.train_mask])

  loss.backward()
  optimizer.step()
  return loss.item()

def rewiring_test(name0, edge_index0, name1, edge_index1, n):
# https: // stackoverflow.com / questions / 11903083 / find - the - set - difference - between - two - large - arrays - matrices - in -python
# https: // stackoverflow.com / questions / 55110047 / finding - non - intersection - of - two - pytorch - tensors
  np_idx0 = edge_index0.numpy().T
  np_idx1 = edge_index1.numpy().T
  rows0 = np.ascontiguousarray(np_idx0).view(np.dtype((np.void, np_idx0.dtype.itemsize * np_idx0.shape[1])))
  rows1 = np.ascontiguousarray(np_idx1).view(np.dtype((np.void, np_idx1.dtype.itemsize * np_idx1.shape[1])))
  # todo use jax.numpy.in1d to do on GPU
  new_added_mask = np.in1d(rows1, rows0, assume_unique=True, invert=True)
  orig_removed_mask = np.in1d(rows0, rows1, assume_unique=True, invert=True)

  orig_edges = np_idx0.shape[0]
  final_edges = np_idx1.shape[0]
  orig_removed = orig_removed_mask.sum()
  orig_retained = len(orig_removed_mask) - orig_removed_mask.sum()
  added = new_added_mask.sum()
  pc_change = (final_edges - orig_edges) / orig_edges
  pc_removed = orig_removed/orig_edges
  pc_retained = orig_retained/orig_edges
  pc_added = added/orig_edges
  edges_div_nodes = final_edges / n

  print(f"Count Original Edges {orig_edges:,}")
  print(f"Count Final Edges {final_edges:,}")
  print(f"Percent Change Final Edges {pc_change:.2%}")
  print(f"Count/Percent Removed Edges {orig_removed:,}, {pc_removed:.2%}")
  print(f"Count/Percent Added Edges {added:,}, {pc_added:.2%}")
  total = orig_edges + added - orig_removed
  print(f"Check: orig {orig_edges:,} + added {added:,} "
        f"- removed {orig_removed:,} = {total:,} == final {final_edges:,}")

  comparison = [name0, name1, orig_edges, final_edges, orig_removed, orig_retained, added,
                pc_change, pc_removed, pc_retained, pc_added, edges_div_nodes]
  return comparison

#todo
# Check robustness to noise
# put a big fully connected layer at the end to check for bottleneck

def rewiring_main(opt, dataset, model_type='GCN', its=10):
  device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
  res_train_acc = []
  res_best_val_acc = []
  res_test_acc = []
  res_T0_dirichlet = []
  res_TN_dirichlet = []
  res_pred_homophil = []
  res_label_homophil = []

  for i in range(its):
    if model_type == "GRAND":
      model = GNN(opt, dataset, device).to(device)
      data = dataset.data.to(device)
      print(opt)
      parameters = [p for p in model.parameters() if p.requires_grad]
      print_model_params(model)
      optimizer = get_optimizer(opt['optimizer'], parameters, lr=opt['lr'], weight_decay=opt['decay'])
      best_val_acc = test_acc = train_acc = best_epoch = 0
      test_fn = test_OGB if opt['dataset'] == 'ogbn-arxiv' else test
      for epoch in range(1, opt['epoch']):
        start_time = time.time()
        loss = train(model, optimizer, data)
        train_acc, val_acc, tmp_test_acc = test_fn(model, data, opt)

        if val_acc > best_val_acc:
          best_val_acc = val_acc
          test_acc = tmp_test_acc
          best_epoch = epoch
        log = 'Epoch: {:03d}, Runtime {:03f}, Loss {:03f}, forward nfe {:d}, backward nfe {:d}, Train: {:.4f}, Val: {:.4f}, Test: {:.4f}'
        print(log.format(epoch, time.time() - start_time, loss, model.fm.sum, model.bm.sum, train_acc, best_val_acc, test_acc))
        print('best val accuracy {:03f} with test accuracy {:03f} at epoch {:d}'.format(best_val_acc, test_acc, best_epoch))

    elif model_type == "GCN":
      model = GCN(opt, dataset).to(device)
      data = dataset.data.to(device)
      print(opt)
      parameters = [p for p in model.parameters() if p.requires_grad]
      print_model_params(model)
      # optimizer = get_optimizer(opt['optimizer'], parameters, lr=opt['lr'], weight_decay=opt['decay'])
      optimizer = rewiring_get_optimizer('adam', model, opt['lr'], opt['decay'])
      best_val_acc = test_acc = train_acc = best_epoch = 0
      test_fn = test_OGB if opt['dataset'] == 'ogbn-arxiv' else test
      for epoch in range(1, opt['epoch']):
        start_time = time.time()
        loss = rewiring_train(model, optimizer, data)
        train_acc, val_acc, tmp_test_acc = test_fn(model, data, opt)

        if val_acc > best_val_acc:
          best_val_acc = val_acc
          test_acc = tmp_test_acc
          best_epoch = epoch
        log = 'Epoch: {:03d}, Runtime {:03f}, Loss {:03f}, Train: {:.4f}, Val: {:.4f}, Test: {:.4f}'
        print(log.format(epoch, time.time() - start_time, loss, train_acc, best_val_acc, test_acc))
        print('best val accuracy {:03f} with test accuracy {:03f} at epoch {:d}'.format(best_val_acc, test_acc, best_epoch))

    #todo check this dirichlet energy implementation: mean(trace(X.t @ L @ X))
    T0_dirichlet = torch.mean(torch.trace(dirichlet_energy(dataset.data.edge_index, dataset.data.edge_attr, dataset.data.num_nodes, dataset.data.x)))
    xN = model(dataset.data.x)
    TN_dirichlet = torch.mean(torch.trace(dirichlet_energy(dataset.data.edge_index, dataset.data.edge_attr, dataset.data.num_nodes, xN)))

    #edge homophilly ratio https://pytorch-geometric.readthedocs.io/en/latest/_modules/torch_geometric/utils/homophily.html#homophily
    pred_homophil = homophily_ratio(edge_index=dataset.data.edge_index, y=xN) #, method='edge')
    label_homophil = homophily_ratio(edge_index=dataset.data.edge_index, y=dataset.data.y) #, method='edge')

    res_train_acc.append(torch.tensor([train_acc]))
    res_best_val_acc.append(torch.tensor([best_val_acc]))
    res_test_acc.append(torch.tensor([test_acc]))
    res_T0_dirichlet.append(T0_dirichlet.unsqueeze(0))
    res_TN_dirichlet.append(TN_dirichlet.unsqueeze(0))
    res_pred_homophil.append(torch.tensor([pred_homophil]))
    res_label_homophil.append(torch.tensor([label_homophil]))

  res_train_acc = torch.cat(res_train_acc)
  res_best_val_acc = torch.cat(res_best_val_acc)
  res_test_acc = torch.cat(res_test_acc)
  res_T0_dirichlet = torch.cat(res_T0_dirichlet)
  res_TN_dirichlet = torch.cat(res_TN_dirichlet)
  res_pred_homophil = torch.cat(res_pred_homophil)
  res_label_homophil = torch.cat(res_label_homophil)

  return res_train_acc.mean().detach().item(), res_best_val_acc.mean().detach().item(), res_test_acc.mean().detach().item(), \
         res_T0_dirichlet.mean().detach().item(), res_TN_dirichlet.mean().detach().item(), res_pred_homophil.mean().detach().item(), res_label_homophil.mean().detach().item(), \
         res_train_acc.std().detach().item(), res_best_val_acc.std().detach().item(), res_test_acc.std().detach().item(), \
         res_T0_dirichlet.std().detach().item(), res_TN_dirichlet.std().detach().item(), res_pred_homophil.std().detach().item(), res_label_homophil.std().detach().item()


def main(opt):
  results = {}
  opt['self_loop_weight'] = None
  dataset = get_dataset(opt, '../data', use_lcc=True)
  n = dataset.data.num_nodes
  edge_index0 = dataset.data.edge_index.detach().clone()

  print(f"edge_index0 contains_self_loops: {contains_self_loops(edge_index0)}")

  opt['exact'] = True
  opt['gdc_sparsification'] = 'topk' #'threshold'
  opt['gdc_threshold'] = 0.01
  # opt['gdc_k'] = 10
  # ppr:
  # hidden_layers: 1
  # hidden_units: 64
  # lr: 0.01
  # dropout: 0.5
  # weight_decay: 0.09604826107599472
  opt['decay'] = 0.05931537406301254 #0.09604826107599472
  # alpha: 0.05
  # k: 128
  # eps:

  edges_stats = rewiring_test("G0", edge_index0, "G0", edge_index0, n)
  train_acc, best_val_acc, test_acc, T0_dirichlet, TN_dirichlet, pred_homophil, label_homophil, \
  sd_train_acc, sd_best_val_acc, sd_test_acc, sd_T0_dirichlet, sd_TN_dirichlet, sd_pred_homophil, sd_label_homophil\
  = rewiring_main(opt, dataset, model_type="GCN")

  results[0] = edges_stats + [train_acc, best_val_acc, test_acc, T0_dirichlet, TN_dirichlet, pred_homophil, label_homophil] \
                + [sd_train_acc, sd_best_val_acc, sd_test_acc, sd_T0_dirichlet, sd_TN_dirichlet, sd_pred_homophil, sd_label_homophil]

  # print('densify..')
  # densified_data = apply_gdc(dataset.data, opt, type = 'densify')
  # edge_index_dense = densified_data.edge_index.detach().clone()
  #
  # edges_stats = rewiring_test("G0", edge_index0, "G_DENSE", edge_index_dense, n)
  # dataset.data = densified_data
  # train_acc, best_val_acc, test_acc, T0_dirichlet, TN_dirichlet, pred_homophil, label_homophil, \
  # sd_train_acc, sd_best_val_acc, sd_test_acc, sd_T0_dirichlet, sd_TN_dirichlet, sd_pred_homophil, sd_label_homophil \
  # = rewiring_main(opt, dataset, model_type="GCN", its=2)
  #
  # results[1] = edges_stats + [train_acc, best_val_acc, test_acc, T0_dirichlet, TN_dirichlet, pred_homophil, label_homophil] \
  #               + [sd_train_acc, sd_best_val_acc, sd_test_acc, sd_T0_dirichlet, sd_TN_dirichlet, sd_pred_homophil, sd_label_homophil]

  # print('sparsify..')
  # ks = [1,2,4,8,16,32,64,128,256]
  # for i,k in enumerate(ks):
  #   opt['gdc_k'] = k
  #   #reset to dense
  #   # dataset.data.edge_index = edge_index_dense
  #   # dataset.data.edge_attr = torch.ones(edge_index_dense.size(1),
  #   #                              device=edge_index_dense.device)
  #   #
  #   # sparsified_data = apply_gdc(dataset.data, opt, type = 'sparsify')
  #
  #   dataset.data.edge_index = edge_index0
  #   dataset.data.edge_attr = torch.ones(edge_index0.size(1),
  #                                device=edge_index0.device)
  #   sparsified_data = apply_gdc(dataset.data, opt, type = 'combined')
  #
  #   dataset.data = sparsified_data
  #   train_acc, best_val_acc, test_acc, T0_dirichlet, TN_dirichlet, pred_homophil, label_homophil, \
  #   sd_train_acc, sd_best_val_acc, sd_test_acc, sd_T0_dirichlet, sd_TN_dirichlet, sd_pred_homophil, sd_label_homophil \
  #   = rewiring_main(opt, dataset, model_type="GCN")
  #
  #   # edges_stats = rewiring_test("GDENSE", edge_index_dense, f"GSPARSE_k{k}", sparsified_data.edge_index, n)
  #   # results[2+2*i] = edges_stats + [train_acc, best_val_acc, test_acc, T0_dirichlet, TN_dirichlet, pred_homophil, label_homophil] \
  #   #             + [sd_train_acc, sd_best_val_acc, sd_test_acc, sd_T0_dirichlet, sd_TN_dirichlet, sd_pred_homophil, sd_label_homophil]
  #
  #   print('overall change..')
  #   edges_stats = rewiring_test("G0", edge_index0, f"GSPARSE_k{k}", sparsified_data.edge_index, n)
  #   results[3+2*i] = edges_stats + [train_acc, best_val_acc, test_acc, T0_dirichlet, TN_dirichlet, pred_homophil, label_homophil] \
  #               + [sd_train_acc, sd_best_val_acc, sd_test_acc, sd_T0_dirichlet, sd_TN_dirichlet, sd_pred_homophil, sd_label_homophil]

  df =  pd.DataFrame.from_dict(results, orient='index',
  columns = ['name0', 'name1', 'orig_edges', 'final_edges', 'orig_removed', 'orig_retained', 'added',
              'pc_change', 'pc_removed', 'pc_retained', 'pc_added', 'edges/nodes',
              'train_acc', 'best_val_acc', 'test_acc',
              'T0_dirichlet', 'TN_av_dirichlet', 'pred_homophil', 'label_homophil',
              'sd_train_acc', 'sd_best_val_acc', 'sd_test_acc',
              'sd_T0_dirichlet', 'sd_TN_dirichlet', 'sd_pred_homophil','sd_label_homophil'])

  print(df)
  df.to_csv('../results/rewiring_G0.csv')

def test_DIGL_data(opt):
  results = {}
  opt['self_loop_weight'] = None
  dataset = get_dataset(opt, '../data', use_lcc=True)
  n = dataset.data.num_nodes
  edge_index0 = dataset.data.edge_index.detach().clone()
  print(f"edge_index0 contains_self_loops: {contains_self_loops(edge_index0)}")

  opt['exact'] = True
  opt['gdc_sparsification'] = 'topk' #'threshold'
  opt['gdc_threshold'] = 0.01
  # opt['gdc_k'] = 10

  # ppr:
  # hidden_layers: 1
  # hidden_units: 64
  # lr: 0.01
  # dropout: 0.5
  # weight_decay: 0.09604826107599472
  opt['decay'] = 0.09604826107599472 #
  # alpha: 0.05
  # k: 128
  # eps:
  opt['use_lcc'] = True

  # PPR_dataset = PPRDataset(
  #           name=opt['dataset'],
  #           use_lcc=opt['use_lcc'],
  #           alpha=opt['ppr_alpha'],
  #           k=opt['gdc_k'],
  #           eps=opt['gdc_threshold'])#'eps'])
  #or
  PPR_dataset = get_dataset(opt, '../data', use_lcc=True)
  PPR_dataset.data = apply_gdc(dataset.data, opt, type='combined')

  edge_indexPPR = PPR_dataset.data.edge_index
  seed = 12345 #1684992425
  num_development = 1500
  device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
  # PPR_dataset.data = set_train_val_test_split(
  #     seed,
  #     dataset.data,
  #     num_development=num_development,
  #   ).to(device)

  edges_stats = rewiring_test("G0", edge_index0, "PPR", edge_indexPPR, n)
  train_acc, best_val_acc, test_acc, T0_dirichlet, TN_dirichlet, pred_homophil, label_homophil, \
  sd_train_acc, sd_best_val_acc, sd_test_acc, sd_T0_dirichlet, sd_TN_dirichlet, sd_pred_homophil, sd_label_homophil\
  = rewiring_main(opt, dataset, model_type="GCN")

  results[0] = edges_stats + [train_acc, best_val_acc, test_acc, T0_dirichlet, TN_dirichlet, pred_homophil, label_homophil] \
                + [sd_train_acc, sd_best_val_acc, sd_test_acc, sd_T0_dirichlet, sd_TN_dirichlet, sd_pred_homophil, sd_label_homophil]


  train_acc, best_val_acc, test_acc, T0_dirichlet, TN_dirichlet, pred_homophil, label_homophil, \
  sd_train_acc, sd_best_val_acc, sd_test_acc, sd_T0_dirichlet, sd_TN_dirichlet, sd_pred_homophil, sd_label_homophil\
  = rewiring_main(opt, PPR_dataset, model_type="GCN")

  results[1] = edges_stats + [train_acc, best_val_acc, test_acc, T0_dirichlet, TN_dirichlet, pred_homophil, label_homophil] \
                + [sd_train_acc, sd_best_val_acc, sd_test_acc, sd_T0_dirichlet, sd_TN_dirichlet, sd_pred_homophil, sd_label_homophil]

  df =  pd.DataFrame.from_dict(results, orient='index',
  columns = ['name0', 'name1', 'orig_edges', 'final_edges', 'orig_removed', 'orig_retained', 'added',
              'pc_change', 'pc_removed', 'pc_retained', 'pc_added', 'edges/nodes',
              'train_acc', 'best_val_acc', 'test_acc',
              'T0_dirichlet', 'TN_av_dirichlet', 'pred_homophil', 'label_homophil',
              'sd_train_acc', 'sd_best_val_acc', 'sd_test_acc',
              'sd_T0_dirichlet', 'sd_TN_dirichlet', 'sd_pred_homophil','sd_label_homophil'])

  print(df)
  df.to_csv('../results/rewiring_PPR.csv')



if __name__ == "__main__":
  parser = argparse.ArgumentParser()

  parser.add_argument('--use_cora_defaults', action='store_true',
                      help='Whether to run with best params for cora. Overrides the choice of dataset')
  # data args
  parser.add_argument('--dataset', type=str, default='Cora',
                      help='Cora, Citeseer, Pubmed, Computers, Photo, CoauthorCS')
  parser.add_argument('--data_norm', type=str, default='rw',
                      help='rw for random walk, gcn for symmetric gcn norm')
  parser.add_argument('--self_loop_weight', type=float, default=1.0, help='Weight of self-loops.')
  parser.add_argument('--use_labels', dest='use_labels', action='store_true', help='Also diffuse labels')
  parser.add_argument('--label_rate', type=float, default=0.5, help='% of training labels to use when --use_labels is set.')
  # GNN args
  parser.add_argument('--hidden_dim', type=int, default=16, help='Hidden dimension.')
  parser.add_argument('--fc_out', dest='fc_out', action='store_true', help='Add a fully connected layer to the decoder.')
  parser.add_argument('--input_dropout', type=float, default=0.5, help='Input dropout rate.')
  parser.add_argument('--dropout', type=float, default=0.0, help='Dropout rate.')
  parser.add_argument("--batch_norm", dest='batch_norm', action='store_true', help='search over reg params')
  parser.add_argument('--optimizer', type=str, default='adam', help='One from sgd, rmsprop, adam, adagrad, adamax.')
  parser.add_argument('--lr', type=float, default=0.01, help='Learning rate.')
  parser.add_argument('--decay', type=float, default=5e-4, help='Weight decay for optimization')
  parser.add_argument('--epoch', type=int, default=50, help='Number of training epochs per iteration.')
  parser.add_argument('--alpha', type=float, default=1.0, help='Factor in front matrix A.')
  parser.add_argument('--alpha_dim', type=str, default='sc', help='choose either scalar (sc) or vector (vc) alpha')
  parser.add_argument('--no_alpha_sigmoid', dest='no_alpha_sigmoid', action='store_true',
                      help='apply sigmoid before multiplying by alpha')
  parser.add_argument('--beta_dim', type=str, default='sc', help='choose either scalar (sc) or vector (vc) beta')
  parser.add_argument('--block', type=str, default='constant', help='constant, mixed, attention, hard_attention, SDE')
  parser.add_argument('--function', type=str, default='laplacian', help='laplacian, transformer, dorsey, GAT, SDE')
  # ODE args
  parser.add_argument('--time', type=float, default=1.0, help='End time of ODE integrator.')
  parser.add_argument('--augment', action='store_true',
                      help='double the length of the feature vector by appending zeros to stabilist ODE learning')
  parser.add_argument('--method', type=str, default='dopri5',
                      help="set the numerical solver: dopri5, euler, rk4, midpoint")
  parser.add_argument('--step_size', type=float, default=1,
                      help='fixed step size when using fixed step solvers e.g. rk4')
  parser.add_argument('--max_iters', type=float, default=100, help='maximum number of integration steps')
  parser.add_argument(
    "--adjoint_method", type=str, default="adaptive_heun",
    help="set the numerical solver for the backward pass: dopri5, euler, rk4, midpoint"
  )
  parser.add_argument('--adjoint', dest='adjoint', action='store_true',
                      help='use the adjoint ODE method to reduce memory footprint')
  parser.add_argument('--adjoint_step_size', type=float, default=1,
                      help='fixed step size when using fixed step adjoint solvers e.g. rk4')
  parser.add_argument('--tol_scale', type=float, default=1., help='multiplier for atol and rtol')
  parser.add_argument("--tol_scale_adjoint", type=float, default=1.0,
                      help="multiplier for adjoint_atol and adjoint_rtol")
  parser.add_argument('--ode_blocks', type=int, default=1, help='number of ode blocks to run')
  parser.add_argument('--add_source', dest='add_source', action='store_true',
                      help='If try get rid of alpha param and the beta*x0 source term')
  # SDE args
  parser.add_argument('--dt_min', type=float, default=1e-5, help='minimum timestep for the SDE solver')
  parser.add_argument('--dt', type=float, default=1e-3, help='fixed step size')
  parser.add_argument('--adaptive', dest='adaptive', action='store_true', help='use adaptive step sizes')
  # Attention args
  parser.add_argument('--leaky_relu_slope', type=float, default=0.2,
                      help='slope of the negative part of the leaky relu used in attention')
  parser.add_argument('--attention_dropout', type=float, default=0., help='dropout of attention weights')
  parser.add_argument('--heads', type=int, default=4, help='number of attention heads')
  parser.add_argument('--attention_norm_idx', type=int, default=0, help='0 = normalise rows, 1 = normalise cols')
  parser.add_argument('--attention_dim', type=int, default=64,
                      help='the size to project x to before calculating att scores')
  parser.add_argument('--mix_features', dest='mix_features', action='store_true',
                      help='apply a feature transformation xW to the ODE')
  parser.add_argument("--max_nfe", type=int, default=1000,
                      help="Maximum number of function evaluations in an epoch. Stiff ODEs will hang if not set.")
  parser.add_argument('--reweight_attention', dest='reweight_attention', action='store_true',
                      help="multiply attention scores by edge weights before softmax")
  # regularisation args
  parser.add_argument('--jacobian_norm2', type=float, default=None, help="int_t ||df/dx||_F^2")
  parser.add_argument('--total_deriv', type=float, default=None, help="int_t ||df/dt||^2")

  parser.add_argument('--kinetic_energy', type=float, default=None, help="int_t ||f||_2^2")
  parser.add_argument('--directional_penalty', type=float, default=None, help="int_t ||(df/dx)^T f||^2")

  # rewiring args
  parser.add_argument('--rewiring', type=str, default=None, help="two_hop, gdc")
  parser.add_argument('--gdc_method', type=str, default='ppr', help="ppr, heat, coeff")
  parser.add_argument('--gdc_sparsification', type=str, default='topk', help="threshold, topk")
  parser.add_argument('--gdc_k', type=int, default=64, help="number of neighbours to sparsify to when using topk")
  parser.add_argument('--gdc_threshold', type=float, default=0.0001,
                      help="obove this edge weight, keep edges when using threshold")
  parser.add_argument('--gdc_avg_degree', type=int, default=64,
                      help="if gdc_threshold is not given can be calculated by specifying avg degree")
  parser.add_argument('--ppr_alpha', type=float, default=0.05, help="teleport probability")
  parser.add_argument('--heat_time', type=float, default=3., help="time to run gdc heat kernal diffusion for")
  parser.add_argument('--att_samp_pct', type=float, default=1,
                      help="float in [0,1). The percentage of edges to retain based on attention scores")
  parser.add_argument('--use_flux', dest='use_flux', action='store_true',
                      help='incorporate the feature grad in attention based edge dropout')
  parser.add_argument("--exact", action="store_true",
                      help="for small datasets can do exact diffusion. If dataset is too big for matrix inversion then you can't")
  parser.add_argument('--M_nodes', type=int, default=64, help="new number of nodes to add")
  parser.add_argument('--new_edges', type=str, default="random", help="random, random_walk, k_hop")
  parser.add_argument('--sparsify', type=str, default="S_hat", help="S_hat, recalc_att")
  parser.add_argument('--threshold_type', type=str, default="addD_rvR", help="topk_adj, addD_rvR")
  parser.add_argument('--rw_addD', type=float, default=0.02, help="percentage of new edges to add")
  parser.add_argument('--rw_rmvR', type=float, default=0.02, help="percentage of edges to remove")


  args = parser.parse_args()
  opt = vars(args)
  main(opt)
  # test_DIGL_data(opt)