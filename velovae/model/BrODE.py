import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import pandas as pd
import os
import sklearn
import time
import matplotlib.pyplot as plt
from cellrank.tl.kernels import PseudotimeKernel

from velovae.plotting import plot_sig, plot_train_loss, plot_test_loss

from .model_util import  histEqual, convertTime, initParams, getTsGlobal, reinitTypeParams, predSU, getGeneIndex
from .model_util import ode_br, optimal_transport_duality_gap, optimal_transport_duality_gap_ts, encode_type, str2int, int2str
from .TrainingData import SCTimedData
from .TransitionGraph import TransGraph
from .velocity import rnaVelocityBrODE



class decoder(nn.Module):
    """
    The ODE model that recovers the input data
    """
    def __init__(self, 
                 adata, 
                 cluster_key,
                 tkey, 
                 embed_key,
                 train_idx,
                 param_key=None,
                 device=torch.device('cpu'), 
                 p=98,
                 checkpoint = None,
                 graph_param = None):
        """
        < Description >
        Constructor of the class
        
        < Input Arguments >
        1.  adata [AnnData]
            Input AnnData object
        
        2.  cluster_key [string]
            Key in adata.obs storing the cell type annotation.
        
        3.  tkey [string]
            Key in adata.obs storing the latent cell time
        
        4.  embed_key [string]
            Key in adata.obsm storing the latent cell state
        
        5.  train_idx [int array]
            The indices of all training samples. We pick 70% of the data as
            training samples by default.
        
        6.  param_key [string]
            Used to extract sigma_u, sigma_s and scaling from adata.var
        
        7.  device [torch device]
            Either cpu or gpu
        
        8.  p [int in (0,100)]
            Percentile to pick steady-state cells.

        9.  checkpoint [string]
            (Optional) Path to a file containing a pretrained model. If given, initialization
            will be skipped and arguments relating to initialization will be ignored.
        
        10. graph_param [dictionary]
            Hyper-parameters for the transition graph computation.
            Keys should contain:
            (1) partition_k: num_neighbors in graph partition (a KNN graph is computed by scanpy)
            (2) partition_res: resolution of Louvain clustering in graph partition
            (3) n_par: number of parents to keep in graph pruning
            (4) dt: tuple (r1,r2), proportion of time range to consider as the parent time window
                Let t_range be the time range. Then for any cell with time t, only cells in the
                time window (t-r2*t_range, t-r1*t_range)
            (5) k: KNN in parent counting.
                This is different from partition_k. When we pick the time window, KNN
                is computed to choose the most likely parents from the cells in the window.
        < Output >
        None. Construct an instance of the class.
        """
        super(decoder,self).__init__()
        
        U,S = adata.layers['Mu'][train_idx], adata.layers['Ms'][train_idx]
        X = np.concatenate((U,S),1) 
        N, G = len(train_idx), adata.n_vars
        
        t = adata.obs[tkey].to_numpy()[train_idx]
        cell_labels_raw = adata.obs[cluster_key].to_numpy()
        self.cell_types = np.unique(cell_labels_raw)
        self.label_dic, self.label_dic_rev = encode_type(self.cell_types)
        cell_labels_int = str2int(cell_labels_raw, self.label_dic)
        cell_labels_int = cell_labels_int[train_idx]
        cell_types_int = str2int(self.cell_types, self.label_dic)
        self.Ntype = len(cell_types_int)
        
        #Transition Graph
        partition_k = graph_param['partition_k'] if 'partition_k' in graph_param else 5
        partition_res = graph_param['partition_res'] if 'partition_res' in graph_param else 0.005
        tgraph = TransGraph(adata, tkey, embed_key, cluster_key, train_idx, k=partition_k, res=partition_res)
        if(graph_param is None):
            w = tgraph.compute_transition_deterministic(adata)
        else:
            w = tgraph.compute_transition_deterministic(adata, graph_param['n_par'], graph_param['dt'], graph_param['k'])
        
        self.w = torch.tensor(w, device=device)
        self.par = torch.argmax(self.w, 1)
        
        #Dynamical Model Parameters
        if(checkpoint is not None):
            self.alpha = nn.Parameter(torch.empty(G, device=device).double())
            self.beta = nn.Parameter(torch.empty(G, device=device).double())
            self.gamma = nn.Parameter(torch.empty(G, device=device).double())
            self.scaling = nn.Parameter(torch.empty(G, device=device).double())
            self.sigma_u = nn.Parameter(torch.empty(G, device=device).double())
            self.sigma_s = nn.Parameter(torch.empty(G, device=device).double())
            
            self.load_state_dict(torch.load(checkpoint, map_location=device))
        else:
            #Dynamical Model Parameters
            U,S = adata.layers['Mu'][train_idx], adata.layers['Ms'][train_idx]
            X = np.concatenate((U,S),1)
            
            print("Initialization using type-specific dynamical model.")

            if(param_key is not None):
                scaling = adata.var[f"{param_key}_scaling"].to_numpy()
                sigma_u = adata.var[f"{param_key}_sigma_u"].to_numpy()
                sigma_s = adata.var[f"{param_key}_sigma_s"].to_numpy()
            else:
                alpha, beta, gamma, scaling, ts, u0, s0, sigma_u, sigma_s, T, Rscore = initParams(X, p, fit_scaling=True)
            
            t_trans, dts = np.zeros((self.Ntype)), np.random.rand(self.Ntype, G)*0.01
            for i, type_ in enumerate(cell_types_int):
                t_trans[type_] = np.quantile(t[cell_labels_int==type_], 0.01)
            ts = t_trans.reshape(-1,1) + dts
            
            alpha, beta, gamma, u0, s0 = reinitTypeParams(U/scaling, S, t, ts, cell_labels_int, cell_types_int, cell_types_int)
            
            self.alpha = nn.Parameter(torch.tensor(np.log(alpha), device=device).double())
            self.beta = nn.Parameter(torch.tensor(np.log(beta), device=device).double())
            self.gamma = nn.Parameter(torch.tensor(np.log(gamma), device=device).double())
            self.t_trans = nn.Parameter(torch.tensor(np.log(t_trans+1e-10), device=device).double())
            self.u0 = nn.Parameter(torch.tensor(np.log(u0), device=device).double())
            self.s0 = nn.Parameter(torch.tensor(np.log(s0), device=device).double())
            self.scaling = nn.Parameter(torch.tensor(np.log(scaling), device=device).double())
            self.sigma_u = nn.Parameter(torch.tensor(np.log(sigma_u), device=device).double())
            self.sigma_s = nn.Parameter(torch.tensor(np.log(sigma_s), device=device).double())
        
        self.t_trans.requires_grad = False
        self.scaling.requires_grad = False
        self.sigma_u.requires_grad = False
        self.sigma_s.requires_grad = False
        self.u0.requires_grad=False
        self.s0.requires_grad=False
        
        
        #self.update_transition_ot(adata.obsm[embed_key][train_idx], t, cell_labels_raw[train_idx], nbin=40, q=0.02)
        #self.update_transition_similarity(adata, tkey, cluster_key)
        
    
    def forward(self, t, y, neg_slope=0.0):
        """
        < Description >
        Evaluate the model in training.
        
        < Input Arguments >
        1.  t [float tensor (N,1)]
        2.  y [int tensor]
        """
        return ode_br(t, 
                      y,
                      self.par,
                      neg_slope=neg_slope,
                      alpha=torch.exp(self.alpha),
                      beta=torch.exp(self.beta),
                      gamma=torch.exp(self.gamma),
                      t_trans=torch.exp(self.t_trans),
                      #ts=torch.exp(self.t_trans.view(-1,1))+torch.exp(self.dts),
                      u0=torch.exp(self.u0),
                      s0=torch.exp(self.s0),
                      sigma_u = torch.exp(self.sigma_u),
                      sigma_s = torch.exp(self.sigma_s),
                      scaling=torch.exp(self.scaling))
    
    def predSU(self, t, y, gidx=None):
        if(gidx is None):
            return ode_br(t, 
                          y,
                          self.par,
                          neg_slope=0.0,
                          alpha=torch.exp(self.alpha),
                          beta=torch.exp(self.beta),
                          gamma=torch.exp(self.gamma),
                          t_trans=torch.exp(self.t_trans),
                          #ts=torch.exp(self.t_trans.view(-1,1))+torch.exp(self.dts),
                          u0=torch.exp(self.u0),
                          s0=torch.exp(self.s0),
                          sigma_u = torch.exp(self.sigma_u),
                          sigma_s = torch.exp(self.sigma_s),
                          scaling=torch.exp(self.scaling))
        return ode_br(t, 
                      y, 
                      self.par,
                      neg_slope=0.0,
                      alpha=torch.exp(self.alpha[:,gidx]),
                      beta=torch.exp(self.beta[:,gidx]),
                      gamma=torch.exp(self.gamma[:,gidx]),
                      t_trans=torch.exp(self.t_trans),
                      #ts=torch.exp(self.t_trans.view(-1,1))+torch.exp(self.dts[:,gidx]),
                      u0=torch.exp(self.u0[:,gidx]),
                      s0=torch.exp(self.s0[:,gidx]),
                      sigma_u = torch.exp(self.sigma_u[gidx]),
                      sigma_s = torch.exp(self.sigma_s[gidx]),
                      scaling=torch.exp(self.scaling[gidx]))
    
    def update_transition_ot(self, 
                             X_embed, 
                             t, 
                             cell_labels, 
                             nbin=20, 
                             epsilon = 0.05, 
                             lambda1 = 1, 
                             lambda2 = 50, 
                             max_iter = 2000, 
                             q = 0.01):
        dt = (t.max()-t.min())/nbin
        
        P = torch.zeros((self.Ntype, self.Ntype), device=self.alpha.device)
        for i, x in enumerate(self.cell_types): #child type
            mask = cell_labels==x
            if(not np.any(mask)):
                P[x,x] = 1.0
                continue
            t0 = np.quantile(t[mask], q) #estimated transition time
            
            mask1 = (t>=t0-dt) & (t<t0) 
            mask2 = (t>=t0) & (t<t0+dt)
            
            if(np.any(mask1) and np.any(mask2)):
                X1, X2 = X_embed[mask1], X_embed[mask2]
                C = sklearn.metrics.pairwise.pairwise_distances(X1,X2,metric='sqeuclidean', n_jobs=-1)
                C = C/np.median(C)
                G = np.ones((C.shape[0]))
                
                Pi = optimal_transport_duality_gap_ts(torch.tensor(C, device=self.alpha.device), 
                                                      torch.tensor(G, device=self.alpha.device), 
                                                      lambda1, lambda2, epsilon, 5, 1e-3, 10000, 1, max_iter)
                
                #Pi_ = optimal_transport_duality_gap(C,G,lambda1, lambda2, epsilon, 5, 0.01, 10000, 1, max_iter)
                
                #Sum the weights of each cell type
                cell_labels_1 = cell_labels[mask1]
                cell_labels_2 = cell_labels[mask2]
                for j, y in enumerate(self.cell_types): #parent
                    if(np.any(cell_labels_1==y) and np.any(cell_labels_2==x)):
                        P[self.label_dic[x],self.label_dic[y]] = torch.sum(Pi[cell_labels_1==y])
            if(P[self.label_dic[x]].sum()==0):
                P[self.label_dic[x],self.label_dic[x]] = 1.0
            
            P[self.label_dic[x]] = P[self.label_dic[x]]/P[self.label_dic[x]].sum()
        
        self.w = P.to(self.alpha.device)
        return
    
    def update_transition_similarity(self, 
                                     adata,
                                     tkey,
                                     cluster_key,
                                     p_thred = 0.95,
                                     q=0.05):
        t = adata.obs[tkey].to_numpy()
        cell_labels = adata.obs[cluster_key].to_numpy()
        P = np.zeros((self.Ntype, self.Ntype))
        for i, x in enumerate(self.cell_types): #child
            t0 = np.quantile(t[cell_labels==x], 0.01)
            dt = (np.quantile(t,0.99) - t.min()) * q
            tmask = (t>=t0-dt) & (t<t0+dt)
            
            vk = PseudotimeKernel(adata[tmask], backward=True, time_key=tkey)
            vk.compute_transition_matrix()
            A = vk.transition_matrix
            for j, y in enumerate(self.cell_types): #parent
                if(np.any(cell_labels[tmask]==y)):
                    P[self.label_dic[x],self.label_dic[y]] = A[cell_labels[tmask]==x][:, cell_labels[tmask]==y].sum()
            if(P[self.label_dic[x]].sum()==0):
                P[self.label_dic[x],self.label_dic[x]] = 1.0
        psum = P.sum(1).reshape(-1,1)
        P = P/psum
        w = np.zeros((len(self.cell_types), len(self.cell_types)))
        #Determine self-transition
        for i, x in enumerate(self.cell_types):
            if(P[self.label_dic[x],self.label_dic[x]] < p_thred):
                P[self.label_dic[x],self.label_dic[x]] = 0.0
                idx_max = np.argmax(P[self.label_dic[x]])
                P[self.label_dic[x]] = P[self.label_dic[x]] / P[self.label_dic[x]].sum()
                
        self.w = torch.tensor(P).double().to(self.alpha.device)



class BrODE():
    """
    Distilled high-level ODE model for RNA velocity with branching structure.
    """
    def __init__(self, 
                 adata, 
                 cluster_key,
                 tkey,
                 embed_key,
                 param_key=None,
                 device='cpu', 
                 checkpoint=None,
                 graph_param=None):
        """
        adata: anndata object
        Tmax: user-defined maximum time for the process
        """
        try:
            U,S = adata.layers['Mu'], adata.layers['Ms']
            cell_labels_raw = adata.obs["clusters"].to_numpy()
            self.cell_types_raw = np.unique(cell_labels_raw)
        except KeyError:
            print('Please run the preprocessing step!')
        
        #Training Configuration
        self.config = {
            #Training Parameters
            "n_epochs":500, 
            "learning_rate":2e-4, 
            "neg_slope":0.0,
            "test_iter":None, 
            "save_epoch":100, 
            "n_update_noise":25,
            "batch_size":128, 
            "early_stop":5,
            "early_stop_thred":adata.n_vars*1e-3,
            "train_test_split":0.7,
            "train_scaling":False, 
            "train_std":False, 
            "weight_sample":False,
            "sparsify":1
        }
        
        self.setDevice(device)
        self.splitTrainTest(adata.n_obs)
        
        
        self.splitTrainTest(adata.n_obs)
        
        N, G = adata.n_obs, adata.n_vars
        
        self.decoder = decoder(adata, 
                               cluster_key,
                               tkey,
                               embed_key,
                               self.train_idx, 
                               param_key,
                               device=self.device, 
                               checkpoint=checkpoint,
                               graph_param=graph_param)
        
        #class attributes for training
        self.loss_train, self.loss_test = [], []
        self.counter = 0 #Count the number of iterations
        self.n_drop = 0 #Count the number of consecutive epochs with negative/low ELBO gain
    
    def setDevice(self, device):
        if('cuda' in device):
            if(torch.cuda.is_available()):
                self.device = torch.device(device)
            else:
                print('Warning: GPU not detected. Using CPU as the device.')
                self.device = torch.device('cpu')
        else:
            self.device = torch.device('cpu')
    
    def splitTrainTest(self, N):
        rand_perm = np.random.permutation(N)
        n_train = int(N*self.config["train_test_split"])
        self.train_idx = rand_perm[:n_train]
        self.test_idx = rand_perm[n_train:]
        
        return
    
    
    
    def forward(self, t, y):
        uhat, shat = self.decoder.forward(t, y, neg_slope=self.config['neg_slope'])
        
        return uhat, shat
    
    def evalModel(self, t, y, gidx=None):
        """
        Run the full model with determinisic parent types.
        """
        uhat, shat = self.decoder.predSU(t, y, gidx)
        
        return uhat, shat
    
    def setMode(self,mode):
        if(mode=='train'):
            self.decoder.train()
        elif(mode=='eval'):
            self.decoder.eval()
        else:
            print("Warning: mode not recognized. Must be 'train' or 'test'! ")
        
    
    ############################################################
    #Training Objective
    ############################################################
    def ODERisk(self,
                u,
                s,
                uhat,
                shat,
                sigma_u, sigma_s, 
                weight=None):
        """
        1. u,s,uhat,shat: raw and predicted counts
        2. sigma_u, sigma_s : standard deviation of the Gaussian likelihood (decoder)
        3. weight: sample weight
        """
    
        neg_log_gaussian = 0.5*((uhat-u)/sigma_u).pow(2)+0.5*((shat-s)/sigma_s).pow(2)+torch.log(sigma_u)+torch.log(sigma_s*2*np.pi)
        
        if( weight is not None):
            neg_log_gaussian = neg_log_gaussian*weight.view(-1,1)
        
        return torch.mean(torch.sum(neg_log_gaussian, 1))
    #ToDo 
    def train_epoch(self, 
                    train_loader, 
                    test_set, 
                    optimizer):
        """
        Training in each epoch
        """
        self.setMode('train')
        stop_training = False
        
        for i, batch in enumerate(train_loader):
            if( self.counter==1 or self.counter % self.config["test_iter"] == 0):
                ll_test = self.test(test_set, self.counter)
                if(len(self.loss_test)>0):
                    if(ll_test - self.loss_test[-1] <= self.config["early_stop_thred"]):
                        self.n_drop = self.n_drop + 1
                    else:
                        self.n_drop = 0
                self.loss_test.append(ll_test)
                self.setMode('train')
                if(self.n_drop>=self.config["early_stop"] and self.config["early_stop"]>0):
                    stop_training=True
                    break
            
            optimizer.zero_grad()
            xbatch, label_batch, tbatch, idx = batch[0].to(self.device), batch[1].to(self.device), batch[2].to(self.device), batch[4]
            u, s = xbatch[:,:xbatch.shape[1]//2],xbatch[:,xbatch.shape[1]//2:]
            
            uhat, shat = self.forward(tbatch, label_batch.squeeze())
            
            loss = self.ODERisk(u, s,
                                uhat, shat,
                                torch.exp(self.decoder.sigma_u), torch.exp(self.decoder.sigma_s))
            loss.backward()
            optimizer.step()
            
            self.loss_train.append(loss.detach().cpu().item())
            self.counter = self.counter + 1
        return stop_training
    
    def loadConfig(self, config):
        #We don't have to specify all the hyperparameters. Just pass the ones we want to modify.
        for key in config:
            if(key in self.config):
                self.config[key] = config[key]
            else:
                self.config[key] = config[key]
                print(f"Added new hyperparameter: {key}")
        if(self.config["train_scaling"]):
            self.decoder.scaling.requires_grad = True
        if(self.config["train_std"]):
            self.decoder.sigma_u.requires_grad = True
            self.decoder.sigma_s.requires_grad = True
    
    def printWeight(self):
        w = self.decoder.w.cpu().numpy()
        with pd.option_context('display.max_rows', None, 
                               'display.max_columns', None, 
                               'display.precision', 3,
                               'display.chop_threshold',1e-3,
                               'display.width', 200):
            w_dic = {}
            cell_types = list(self.decoder.label_dic.keys())
            for i in range(self.decoder.Ntype):
                w_dic[self.decoder.label_dic_rev[i]] = w[:, i]
            w_df = pd.DataFrame(w_dic, index=pd.Index(cell_types))
            print(w_df)
    
    def update_std_noise(self, train_set):
        G = train_set.G
        Uhat, Shat, ll = self.predAll(train_set.data, torch.tensor(train_set.time).double().to(self.device), train_set.labels, train_set.N, train_set.G, np.array(range(G)))
        self.decoder.sigma_u = nn.Parameter(torch.tensor(np.log((Uhat-train_set.data[:,:G]).std(0)+1e-10), device=self.device))
        self.decoder.sigma_s = nn.Parameter(torch.tensor(np.log((Shat-train_set.data[:,G:]).std(0)+1e-10), device=self.device))
        return
    
    def train(self, 
              adata, 
              tkey,
              cluster_key,
              config={}, 
              plot=False, 
              gene_plot=[], 
              figure_path="figures", 
              embed="umap"):
        self.tkey = tkey
        self.cluster_key = cluster_key
        self.loadConfig(config)
        
        if(self.config["train_scaling"]):
            self.decoder.scaling.requires_grad = True
        if(self.config["train_std"]):
            self.decoder.sigma_u.requires_grad = True
            self.decoder.sigma_s.requires_grad = True
        
        print("------------------------ Train a Branching ODE ------------------------")
        #Get data loader
        X = np.concatenate((adata.layers['Mu'], adata.layers['Ms']), 1)
        X = X.astype(float)
        try:
            Xembed = adata.obsm[f"X_{embed}"]
        except KeyError:
            print("Embedding not found! Please run the corresponding preprocessing step!")
        cell_labels_raw = adata.obs[cluster_key].to_numpy() if cluster_key in adata.obs else np.array(['Unknown' for i in range(adata.n_obs)])
        cell_labels = str2int(cell_labels_raw, self.decoder.label_dic)
        t = adata.obs[tkey].to_numpy()
        
        self.printWeight()
        
        print("*********        Creating Training/Validation Datasets        *********")
        train_set = SCTimedData(X[self.train_idx], cell_labels[self.train_idx], t[self.train_idx])
        test_set = None
        if(len(self.test_idx)>0):
            test_set = SCTimedData(X[self.test_idx], cell_labels[self.test_idx], t[self.test_idx])
        data_loader = torch.utils.data.DataLoader(train_set, batch_size=self.config["batch_size"], shuffle=True)
        #Automatically set test iteration if not given
        if(self.config["test_iter"] is None):
            self.config["test_iter"] = len(self.train_idx)//self.config["batch_size"]*2
        print("*********                      Finished.                      *********")
        
        gind, gene_plot = getGeneIndex(adata.var_names, gene_plot)
        os.makedirs(figure_path, exist_ok=True)
    
        #define optimizer
        print("*********                 Creating optimizers                 *********")
        param_ode = [self.decoder.alpha, self.decoder.beta, self.decoder.gamma,
                     self.decoder.t_trans, self.decoder.u0, self.decoder.s0]
        if(self.config["train_scaling"]):
            param_ode = param_ode+[self.decoder.scaling]
        if(self.config["train_std"]):
            param_ode = param_ode+[self.decoder.sigma_u, self.decoder.sigma_s]
    
        optimizer = torch.optim.Adam(param_ode, lr=self.config["learning_rate"])
        print("*********                      Finished.                      *********")
        
        #Main Training Process
        print("*********                    Start training                   *********")
        print(f"Total Number of Iterations Per Epoch: {len(data_loader)}, test iteration: {self.config['test_iter']}")
        
        n_epochs = self.config["n_epochs"]
        
        start = time.time()
        
        for epoch in range(n_epochs):
            stop_training = self.train_epoch(data_loader, test_set, optimizer)
            
            if(plot and (epoch==0 or (epoch+1) % self.config["save_epoch"] == 0)):
                ll_train = self.test(train_set,
                                     f"train{epoch+1}", 
                                     gind, 
                                     gene_plot,
                                     True, 
                                     figure_path)
                self.setMode('train')
                ll = -np.inf if len(self.loss_test)==0 else self.loss_test[-1]
                print(f"Epoch {epoch+1}: Train Log Likelihood = {ll_train:.3f}, Test Log Likelihood = {ll:.3f}, \t Total Time = {convertTime(time.time()-start)}")
            
            if((epoch+1) % self.config["n_update_noise"] == 0):
                self.update_std_noise(train_set)
                
            if(stop_training):
                print(f"*********           Early Stop Triggered at epoch {epoch+1}.            *********")
                break
        
        print(f"*********              Finished. Total Time = {convertTime(time.time()-start)}             *********")
        plot_train_loss(self.loss_train, range(1,len(self.loss_train)+1), save=f'{figure_path}/train_loss_brode.png')
        if(self.config["test_iter"]>0):
            plot_test_loss(self.loss_test, [i*self.config["test_iter"] for i in range(1,len(self.loss_test)+1)], save=f'{figure_path}/test_loss_brode.png')
        return
    
    #ToDo 
    def predAll(self, data, t, cell_labels, N, G, gene_idx=None):
        """
        Input Arguments:
        1. data [N x 2G] : input mRNA count
        2. mode : train or test or both
        3. gene_idx : gene index, used for reducing unnecessary memory usage
        """
        if(gene_idx is None):
            Uhat, Shat = None, None
        else:
            Uhat, Shat = np.zeros((N, len(gene_idx))), np.zeros((N, len(gene_idx)))
        ll = 0
        with torch.no_grad():
            B = min(N//5, 5000)
            Nb = N // B
            for i in range(Nb):
                uhat, shat = self.evalModel(t[i*B:(i+1)*B], torch.tensor(cell_labels[i*B:(i+1)*B]).to(self.device))
                if(gene_idx is not None):
                    Uhat[i*B:(i+1)*B] = uhat[:, gene_idx].cpu().numpy()
                    Shat[i*B:(i+1)*B] = shat[:, gene_idx].cpu().numpy()
                loss = self.ODERisk(torch.tensor(data[i*B:(i+1)*B, :G]).double().to(self.device),
                                    torch.tensor(data[i*B:(i+1)*B, G:]).double().to(self.device),
                                    uhat, shat,
                                    torch.exp(self.decoder.sigma_u), torch.exp(self.decoder.sigma_s))
                ll = ll - (B/N)*loss
            if(N > B*Nb):
                uhat, shat = self.evalModel(t[B*Nb:], torch.tensor(cell_labels[B*Nb:]).to(self.device))
                if(gene_idx is not None):
                    Uhat[Nb*B:] = uhat[:, gene_idx].cpu().numpy()
                    Shat[Nb*B:] = shat[:, gene_idx].cpu().numpy()
                loss = self.ODERisk(torch.tensor(data[B*Nb:, :G]).double().to(self.device),
                                    torch.tensor(data[B*Nb:, G:]).double().to(self.device),
                                    uhat, shat,
                                    torch.exp(self.decoder.sigma_u), torch.exp(self.decoder.sigma_s))
                ll = ll - ((N-B*Nb)/N)*loss
        return Uhat, Shat, ll.cpu().item()
    
    #ToDo 
    def test(self,
             dataset, 
             testid=0, 
             gind=None,
             gene_plot=[],
             plot=False,
             path='figures', 
             **kwargs):
        """
        data: ncell x ngene tensor
        """
        
        self.setMode('eval')
        
        Uhat, Shat, ll = self.predAll(dataset.data, torch.tensor(dataset.time).double().to(self.device), dataset.labels, dataset.N, dataset.G, gind)
        
        cell_labels_raw = int2str(dataset.labels, self.decoder.label_dic_rev)
        if(plot):
            for i in range(len(gene_plot)):
                idx = gind[i]
                plot_sig(dataset.time.squeeze(), 
                         dataset.data[:,idx], dataset.data[:,idx+dataset.G],
                         Uhat[:,i], Shat[:,i], 
                         cell_labels_raw,
                         gene_plot[i],
                         save=f"{path}/sig-{gene_plot[i]}-{testid}.png",
                         sparsify=self.config["sparsify"],
                         t_trans=self.decoder.t_trans.detach().cpu().exp().numpy())
                
        return ll
    
    #ToDo 
    def saveModel(self, file_path, name='brode'):
        """
        Save the decoder parameters to a .pt file.
        """
        os.makedirs(file_path, exist_ok=True)
        torch.save(self.decoder.state_dict(), f"{file_path}/{name}.pt")
    #ToDo 
    def saveAnnData(self, adata, key, file_path, file_name=None):
        """
        Save the ODE parameters and cell time to the anndata object and write it to disk.
        """
        self.setMode('eval')
        os.makedirs(file_path, exist_ok=True)
        
        X = np.concatenate((adata.layers['Mu'], adata.layers['Ms']), 1)
        t = adata.obs[self.tkey].to_numpy()
        label_int = str2int(adata.obs[self.cluster_key].to_numpy(),self.decoder.label_dic)
        
        adata.obs[f"{key}_time"] = t
        adata.obs[f"{key}_label"] = label_int
        adata.varm[f"{key}_alpha"] = np.exp(self.decoder.alpha.detach().cpu().numpy()).T
        adata.varm[f"{key}_beta"] = np.exp(self.decoder.beta.detach().cpu().numpy()).T
        adata.varm[f"{key}_gamma"] = np.exp(self.decoder.gamma.detach().cpu().numpy()).T
        #adata.varm[f"{key}_ts"] = np.exp(self.decoder.dts.detach().cpu().numpy()).T + np.exp(self.decoder.t_trans.detach().cpu().numpy())
        adata.uns[f"{key}_t_trans"] = np.exp(self.decoder.t_trans.detach().cpu().numpy())
        adata.varm[f"{key}_u0"] = np.exp(self.decoder.u0.detach().cpu().numpy()).T
        adata.varm[f"{key}_s0"] = np.exp(self.decoder.s0.detach().cpu().numpy()).T
        adata.var[f"{key}_scaling"] = np.exp(self.decoder.scaling.detach().cpu().numpy())
        adata.var[f"{key}_sigma_u"] = np.exp(self.decoder.sigma_u.detach().cpu().numpy())
        adata.var[f"{key}_sigma_s"] = np.exp(self.decoder.sigma_s.detach().cpu().numpy())
        adata.uns[f"{key}_w"] = self.decoder.w.detach().cpu().numpy()
        
        Uhat, Shat, ll = self.predAll(X, 
                                      torch.tensor(t.reshape(-1,1)).to(self.device), 
                                      label_int, 
                                      adata.n_obs, 
                                      adata.n_vars, 
                                      np.array(range(adata.n_vars)))
        adata.layers[f"{key}_uhat"] = Uhat
        adata.layers[f"{key}_shat"] = Shat
        
        adata.uns[f"{key}_train_idx"] = self.train_idx
        adata.uns[f"{key}_test_idx"] = self.test_idx
        adata.uns[f"{key}_label_dic"] = self.decoder.label_dic
        #adata.uns[f"{key}_label_dic_rev"] = self.decoder.label_dic_rev
        
        rnaVelocityBrODE(adata, key)
        
        if(file_name is not None):
            adata.write_h5ad(f"{file_path}/{file_name}")