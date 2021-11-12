import numpy as np
import anndata
import scvelo as scv

def run_scv(filename, Ngene, Nplot):
    adata = anndata.read_h5ad(filename)
    #Preprocessing
    #1. Gene filtering and data normalization
    scv.pp.filter_and_normalize(adata, min_shared_counts=100, min_shared_cells = 100,  n_top_genes=Ngene)
    #2. KNN Averaging
    scv.pp.moments(adata,n_pcs=30, n_neighbors=50)
    #3. Obtain cell clusters
    if(not 'clusters' in adata.obs):
        if('Class' in adata.obs):
            adata.obs['clusters'] = adata.obs['Class'].to_numpy()
        else:
            scanpy.tl.leiden(adata, key_added='clusters')
            print(np.unique(adata.obs['clusters'].to_numpy()))
    
    
    #4. Compute Umap coordinates for visulization
    #if(not 'X_umap' in adata.obsm):
    scv.tl.umap(adata)
    #Fit each gene
    scv.tl.recover_dynamics(adata)

    #Compute velocity, time and velocity graph (KNN graph based on velocity)
    scv.tl.velocity(adata, mode='dynamical')
    scv.tl.latent_time(adata)
    scv.tl.velocity_graph(adata, vkey="velocity", tkey="fit_t", gene_subset=adata.var_names[adata.var["velocity_genes"]])
    
    #Plotting
    top_genes = adata.var['fit_likelihood'].sort_values(ascending=False).index
    for i in range(Nplot):
        scv.pl.scatter(adata, basis=[top_genes[i]],linewidth=2.0,figsize=(12,8),add_assignments=True,save=f"{top_genes[i]}.png")
    
    scv.pl.velocity_embedding_stream(adata, basis='umap', vkey="velocity", save=f"vel-stream.png")
    scv.pl.scatter(adata, color='latent_time', color_map='gnuplot', size=80, colorbar=True, save=f"time.png")
    
    #Save the output
    adata.write_h5ad(filename)
    
    scv.pl.scatter(adata, basis='X_umap', figsize=(10,10), save='class.png')
    return

def scvPlot(filename, genes):
    adata = anndata.read_h5ad(filename)
    
    scv.pl.scatter(adata, x='latent_time', y=genes, legend_loc='right margin', frameon=False, save="genes_global.png")
    scv.pl.scatter(adata, x='fit_t', y=genes, legend_loc='right margin', frameon=False, save="genes.png")
    #age = adata.obs['Age'].to_numpy()
    #tprior = np.array([float(x[1:]) for x in age])
    #adata.obs['tprior'] = tprior
    #scv.pl.scatter(adata, basis='X_umap', color='tprior', cmap='gnuplot', save='tprior.png')
    
    return

filename = "/scratch/blaauw_root/blaauw1/gyichen/braindev_part.h5ad"
Ngene = 1000
Nplot = 10
run_scv(filename, Ngene, Nplot)
#genes = ['Auts2', 'Dync1i1', 'Gm3764', 'Mapt', 'Nfib', 'Rbfox1', 'Satb2', 'Slc6a13', 'Srrm4', 'Tcf4']
#scvPlot(filename, genes)