# -*- coding: utf-8 -*-
"""
Created on Mon Dec  4 15:27:38 2017

@author: ben
"""
import numpy as np
from LSsurf.lin_op import lin_op
import scipy.sparse as sp
from LSsurf.data_slope_bias import data_slope_bias
import sparseqr
from time import time, ctime
from LSsurf.RDE import RDE
#import LSsurf.op_structure_checks as checks
import pointCollection as pc
import scipy.optimize as scipyo
from scipy.stats import scoreatpercentile
from LSsurf.inv_tr_upper import inv_tr_upper
from LSsurf.smooth_xytb_fit import setup_grids, assign_bias_ID, \
                                    setup_mask, setup_smoothness_constraints,\
                                    setup_averaging_ops, setup_avg_mask_ops,\
                                    build_reference_epoch_matrix, setup_bias_fit
from LSsurf.match_priors import match_prior_dz

def check_data_against_DEM(in_TSE, data, m0, G_data, DEM_tol):
    m1 = m0.copy()
    m1[G_data.TOC['cols']['z0']]=0
    r_DEM=data.z - G_data.toCSR().dot(m1) - data.DEM
    temp=in_TSE
    temp[in_TSE] = np.abs(r_DEM[in_TSE]) < DEM_tol
    return temp

def calc_sigma_extra(r, sigma):
    '''
    calculate the error needed to be added to the data to achieve RDE(rs)==1

    Parameters
    ----------
    r : numpy array
        model residuals
    sigma : numpy array
        estimated errors

    Returns
    -------
    sigma_extra.

    '''
    sigma_hat=RDE(r)
    sigma_aug_minus_1_sq = lambda sigma1: (RDE(r/np.sqrt(sigma1**2+sigma**2))-1)**2
    sigma_extra=scipyo.minimize_scalar(sigma_aug_minus_1_sq, method='bounded', bounds=[0, sigma_hat])['x']
    return sigma_extra

def edit_by_bias(data, m0, in_TSE, iteration, bias_model, args):

    if args['bias_nsigma_edit'] is None:
        return False

    # assign the edited field in bias_model['bias_param_dict'] if needed
    if 'edited' not in bias_model['bias_param_dict']:
            bias_model['bias_param_dict']['edited']=np.zeros_like(bias_model['bias_param_dict']['ID'], dtype=bool)
    bias_dict, slope_bias_dict=parse_biases(m0, bias_model, args['bias_params'])
    bias_scaled = np.abs(bias_dict['val']) / np.array(bias_dict['expected'])

    last_edit = bias_model['bias_param_dict']['edited'].copy()
    bad_bias = np.zeros_like(bias_scaled, dtype=bool)
    bad_bias |= last_edit
    if iteration >= args['bias_nsigma_iteration']:
        extreme_bias_scaled_threshold = np.minimum(50, 3*scoreatpercentile(bias_scaled, 95))
        if np.any(bias_scaled > extreme_bias_scaled_threshold):
            bad_bias[ bias_scaled == np.max(bias_scaled) ] = True
        else:
            bad_bias[bias_scaled > args['bias_nsigma_edit']] = True

    bad_bias_IDs=np.array(bias_dict['ID'])[bad_bias]

    for ID in bad_bias_IDs:
        #Mark each bad ID as edited (because it will have a bias estimate of zero in subsequent iterations)
        bias_model['bias_param_dict']['edited'][bias_model['bias_param_dict']['ID'].index(ID)]=True
    if len(bad_bias_IDs)>0:
        print(f"\t have {len(bad_bias_IDs)} bad biases, with {np.sum(np.in1d(data.bias_ID, bad_bias_IDs))} data.")
    in_TSE[np.in1d(data.bias_ID, bad_bias_IDs)]=False

    return  ~np.all(bias_model['bias_param_dict']['edited'] == last_edit)

def iterate_fit(data, Gcoo, rhs, TCinv, G_data, Gc, in_TSE, Ip_c, timing, args,
                bias_model=None):
    cov_rows=G_data.N_eq+np.arange(Gc.N_eq)
    E_all = 1/TCinv.diagonal()

    # run edit_by_bias to zero out the edited IDs
    edit_by_bias(data, np.zeros(Ip_c.shape[0]), in_TSE, -1, bias_model, args)

    #print(f"iterate_fit: G.shape={Gcoo.shape}, G.nnz={Gcoo.nnz}, data.shape={data.shape}", flush=True)
    in_TSE_original=np.zeros(data.shape, dtype=bool)
    in_TSE_original[in_TSE]=True

    sigma_extra_pre=0
    sigma_extra=0
    if 'tide_ocean' in data.fields:
        early_shelf = (data.time < 2009) & (data.tide_ocean != 0)
    else:
        early_shelf=np.zeros_like(data.x, dtype=bool)
    not_early_shelf = ~early_shelf
    N_eq = Gcoo.shape[0]
    last_iteration = False
    m0 = np.zeros(Ip_c.shape[0])
    for iteration in range(args['max_iterations']):

        # augment the errors on the shelf
        E2_plus=E_all**2
        E2_plus[np.nonzero(early_shelf)] += sigma_extra_pre**2
        if last_iteration:
            E2_plus[np.nonzero(not_early_shelf)] += sigma_extra**2
        TCinv=sp.dia_matrix((1./np.sqrt(E2_plus),0), shape=(N_eq, N_eq))

        # build the parsing matrix that removes invalid rows
        Ip_r=sp.coo_matrix((np.ones(Gc.N_eq+in_TSE.sum()), \
                            (np.arange(Gc.N_eq+in_TSE.sum()), \
                             np.concatenate((np.flatnonzero(in_TSE), cov_rows)))), \
                           shape=(Gc.N_eq+in_TSE.sum(), Gcoo.shape[0])).tocsc()

        if args['VERBOSE']:
            print("starting qr solve for iteration %d at %s" % (iteration, ctime()), flush=True)
        # solve the equations
        tic=time();
        m0_last=m0
        m0=Ip_c.dot(sparseqr.solve(Ip_r.dot(TCinv.dot(Gcoo)), Ip_r.dot(TCinv.dot(rhs))));
        timing['sparseqr_solve']=time()-tic

        # calculate the full data residual
        r_data=data.z-G_data.toCSR().dot(m0)
        rs_data=r_data/data.sigma
        if last_iteration:
            break
        # calculate the additional error needed to make the robust spread of the scaled residuals equal to 1
        sigma_extra_pre=calc_sigma_extra(r_data[in_TSE & early_shelf], data.sigma[in_TSE & early_shelf])
        sigma_extra=calc_sigma_extra(r_data[in_TSE & not_early_shelf], data.sigma[in_TSE & not_early_shelf])
        # augmented sigma
        sigma_aug2 = data.sigma**2
        sigma_aug2[early_shelf] += sigma_extra_pre**2
        sigma_aug2[not_early_shelf] += sigma_extra**2
        sigma_aug=np.sqrt(sigma_aug2)
        # select the data that have scaled residuals < 3 *max(1, sigma_hat)
        in_TSE_last=in_TSE

        in_TSE = np.abs(r_data/sigma_aug) < 3.0

        # if bias_nsigma_edit is specified, check for biases that are more than
        # args['bias_nsigma_edit'] times their expected values.
        bias_editing_changed=edit_by_bias(data, m0, in_TSE, iteration, bias_model, args)
        if 'editable' in data.fields:
            in_TSE[data.editable==0] = in_TSE_original[data.editable==0]

        if args['DEM_tol'] is not None:
            in_TSE = check_data_against_DEM(in_TSE, data, m0, G_data, args['DEM_tol'])

        # quit if the solution is too similar to the previous solution
        if (np.max(np.abs((m0_last-m0)[Gc.TOC['cols']['dz']])) < args['converge_tol_dz']) and (iteration > 2):
            if args['VERBOSE']:
                print("Solution identical to previous iteration with tolerance %3.1f, exiting after iteration %d" % (args['converge_tol_dz'], iteration))
            last_iteration = True
        # select the data that are within 3*sigma of the solution
        if args['VERBOSE']:
            print('found %d in TSE, sigma_extra_pre = %3.3f, sigma_extra=%3.3f,  dt=%3.0f' % ( in_TSE.sum(), sigma_extra_pre, sigma_extra, timing['sparseqr_solve']), flush=True)
        if iteration > 0  and iteration > args['bias_nsigma_iteration']:
            if np.all( in_TSE_last == in_TSE ):
                if args['VERBOSE']:
                    print("filtering unchanged, exiting after iteration %d" % iteration)
                last_iteration=True
        if iteration >= np.maximum(2, args['bias_nsigma_iteration']+1):
            if sigma_extra < 0.5 *np.min(data.sigma[in_TSE]) and not bias_editing_changed:
                if args['VERBOSE']:
                    print("sigma_0==0, exiting after iteration %d" % iteration, flush=True)
                last_iteration=True
        if iteration==args['max_iterations']-2:
            last_iteration=True

    return m0, sigma_extra, in_TSE, rs_data

def parse_biases(m, bias_model, bias_params):
    """
        parse the biases in the ouput model

        inputs:
            m: model vector
            bias_model: the bias model
            bias_params: a list of parameters for which biases are calculated
        output:
            b_dict: a dictionary giving the parameters and associated bias values for each ibas ID
            slope_bias_dict:  a dictionary giving the parameters and assicated biase values for each slope bias ID
    """
    slope_bias_dict={}
    b_dict={param:list() for param in bias_params+['val','ID','expected']}
    # loop over the keys in bias_model['bias_ID_dict']
    for item in bias_model['bias_ID_dict']:
        b_dict['val'].append(m[bias_model['bias_ID_dict'][item]['col']])
        b_dict['ID'].append(item)
        b_dict['expected'].append(bias_model['E_bias'][item])
        for param in bias_params:
            b_dict[param].append(bias_model['bias_ID_dict'][item][param])
    if 'slope_bias_dict' in bias_model:
        for key in bias_model['slope_bias_dict']:
            slope_bias_dict[key]={'slope_x':m[bias_model['slope_bias_dict'][key][0]], 'slope_y':m[bias_model['slope_bias_dict'][key][1]]}
    return b_dict, slope_bias_dict

def calc_and_parse_errors(E, Gcoo, TCinv, rhs, Ip_c, Ip_r, grids, G_data, Gc, avg_ops, bias_model, bias_params, dzdt_lags=None, timing={}, error_res_scale=None):
    tic=time()
    # take the QZ transform of Gcoo  # TEST WHETHER rhs can just be a vector of ones
    z, R, perm, rank=sparseqr.rz(Ip_r.dot(TCinv.dot(Gcoo)), Ip_r.dot(TCinv.dot(rhs)))
    z=z.ravel()
    R=R.tocsr()
    R.sort_indices()
    R.eliminate_zeros()
    timing['decompose_qz']=time()-tic

    E0=np.zeros(R.shape[0])

    # compute Rinv for use in propagating errors.
    # what should the tolerance be?  We will eventually square Rinv and take its
    # row-wise sum.  We care about errors at the cm level, so
    # size(Rinv)*tol^2 = 0.01 -> tol=sqrt(0.01/size(Rinv))~ 1E-4
    tic=time(); RR, CC, VV, status=inv_tr_upper(R, np.int(np.prod(R.shape)/4), 1.e-5);
    # save Rinv as a sparse array.  The syntax perm[RR] undoes the permutation from QZ
    Rinv=sp.coo_matrix((VV, (perm[RR], CC)), shape=R.shape).tocsr(); timing['Rinv_cython']=time()-tic;
    tic=time(); E0=np.sqrt(Rinv.power(2).sum(axis=1)); timing['propagate_errors']=time()-tic;

    # generate the full E vector.  E0 appears to be an ndarray,
    E0=np.array(Ip_c.dot(E0)).ravel()
    E['sigma_z0']=pc.grid.data().from_dict({'x':grids['z0'].ctrs[1],\
                                     'y':grids['z0'].ctrs[0],\
                                    'sigma_z0':np.reshape(E0[Gc.TOC['cols']['z0']], grids['z0'].shape)})
    E['sigma_dz']=pc.grid.data().from_dict({'x':grids['dz'].ctrs[1],\
                                     'y':grids['dz'].ctrs[0],\
                                    'time':grids['dz'].ctrs[2],\
                                    'sigma_dz': np.reshape(E0[Gc.TOC['cols']['dz']], grids['dz'].shape)})

    # generate the lagged dz errors: [CHECK THIS]
    for key, op in avg_ops.items():
        E['sigma_'+key] = pc.grid.data().from_dict({'x':op.dst_grid.ctrs[1], \
                                          'y':op.dst_grid.ctrs[0], \
                                        'time': op.dst_grid.ctrs[2], \
                                            'sigma_'+key: op.grid_error(Ip_c.dot(Rinv))})

    # generate the grid-mean error for zero lag
    if len(bias_model.keys()) >0:
        E['sigma_bias'], E['sigma_slope_bias'] = parse_biases(E0, bias_model, bias_params)

def parse_model(m, m0, data, R, RMS, G_data, averaging_ops, Gc, Ec, grids, bias_model, args):

    # reshape the components of m to the grid shapes
    m['z0']=pc.grid.data().from_dict({'x':grids['z0'].ctrs[1],\
                                     'y':grids['z0'].ctrs[0],\
                                     'cell_area': grids['z0'].cell_area, \
                                     'mask':grids['z0'].mask, \
                                     'z0':np.reshape(m0[G_data.TOC['cols']['z0']], grids['z0'].shape)})
    m['dz']=pc.grid.data().from_dict({'x':grids['dz'].ctrs[1],\
                                     'y':grids['dz'].ctrs[0],\
                                     'time':grids['dz'].ctrs[2],\
                                     'cell_area':grids['dz'].cell_area, \
                                     'mask':grids['dz'].mask, \
                                     'dz': np.reshape(m0[G_data.TOC['cols']['dz']], grids['dz'].shape)})
    if 'PS_bias' in G_data.TOC['cols']:
        m['dz'].assign({'PS_bias':np.reshape(m0[G_data.TOC['cols']['PS_bias']], grids['dz'].shape[0:2])})

    # calculate height rates and averages
    for key, op  in averaging_ops.items():
        m[key] = pc.grid.data().from_dict({'x':op.dst_grid.ctrs[1], \
                                          'y':op.dst_grid.ctrs[0], \
                                        'time': op.dst_grid.ctrs[2], \
                                        'cell_area':op.dst_grid.cell_area,\
                                            key: op.grid_prod(m0)})

    # report the parameter biases.  Sorted in order of the parameter bias arguments
    if len(bias_model.keys()) > 0:
        m['bias'], m['slope_bias']=parse_biases(m0, bias_model, args['bias_params'])

    # report the entire model vector, just in case we want it.
    m['all']=m0

    # report the geolocation of the output map
    m['extent']=np.concatenate((grids['z0'].bds[1], grids['z0'].bds[0]))

    # parse the resduals to assess the contributions of the total error:
    # Make the C matrix for the constraints
    TCinv_cov=sp.dia_matrix((1./Ec, 0), shape=(Gc.N_eq, Gc.N_eq))
    # scaled residuals
    rc=TCinv_cov.dot(Gc.toCSR().dot(m0))
    # unscaled residuals
    ru=Gc.toCSR().dot(m0)
    for eq_type in ['d2z_dt2','grad2_z0','grad2_dzdt','grad2_PS']:
        if eq_type in Gc.TOC['rows']:
            R[eq_type]=np.sum(rc[Gc.TOC['rows'][eq_type]]**2)
            RMS[eq_type]=np.sqrt(np.mean(ru[Gc.TOC['rows'][eq_type]]**2))
    r=(data.z-data.z_est)[data.three_sigma_edit]
    r_scaled=r/data.sigma[data.three_sigma_edit]
    for ff in ['dz','z0']:
        m[ff].assign({'count':G_data.toCSR()[:,G_data.TOC['cols'][ff]][data.three_sigma_edit,:].T.\
                        dot(np.ones_like(r)).reshape(grids[ff].shape)})
        m[ff].count[m[ff].count==0]=np.NaN
        m[ff].assign({'misfit_scaled_rms':np.sqrt(G_data.toCSR()[:,G_data.TOC['cols'][ff]][data.three_sigma_edit,:].T.dot(r_scaled**2)\
                                        .reshape(grids[ff].shape)/m[ff].count)})
        m[ff].assign({'misfit_rms':np.sqrt(G_data.toCSR()[:,G_data.TOC['cols'][ff]][data.three_sigma_edit,:].T.dot(r**2)\
                                         .reshape(grids[ff].shape)/m[ff].count)})
        if 'tide' in data.fields:
            r_notide=(data.z+data.tide-data.z_est)[data.three_sigma_edit]
            r_notide_scaled=r_notide/data.sigma[data.three_sigma_edit]
            m[ff].assign({'misfit_notide_rms':np.sqrt(G_data.toCSR()[:,G_data.TOC['cols'][ff]][data.three_sigma_edit,:].T.dot(r_notide**2)\
                                        .reshape(grids[ff].shape)/m[ff].count)})
            m[ff].assign({'misfit_notide_scaled_rms':np.sqrt(G_data.toCSR()[:,G_data.TOC['cols'][ff]][data.three_sigma_edit,:].T.dot(r_notide_scaled**2)\
                                        .reshape(grids[ff].shape)/m[ff].count)})

def smooth_xytb_fit_aug(**kwargs):
    required_fields=('data','W','ctr','spacing','E_RMS')
    args={'reference_epoch':0,
    'W_ctr':1e4,
    'mask_file':None,
    'mask_data':None,
    'mask_scale':None,
    'compute_E':False,
    'max_iterations':10,
    'srs_proj4': None,
    'N_subset': None,
    'bias_params': None,
    'bias_filter':None,
    'repeat_res':None,
    'converge_tol_dz':0.05,
    'DEM_tol':None,
    'repeat_dt': 1,
    'Edit_only': False,
    'dzdt_lags':None,
    'prior_args':None,
    'avg_scales':[],
    'data_slope_sensors':None,
    'E_slope':0.05,
    'E_RMS_d2x_PS_bias':None,
    'E_RMS_PS_bias':None,
    'error_res_scale':None,
    'avg_masks':None,
    'bias_nsigma_edit':None,
    'bias_nsigma_iteration':2,
    'bias_edit_vals':None,
    'VERBOSE': True}
    args.update(kwargs)
    for field in required_fields:
        if field not in kwargs:
            raise ValueError("%s must be defined", field)
    valid_data = np.isfinite(args['data'].z) #np.ones_like(args['data'].x, dtype=bool)
    timing=dict()

    m={}
    E={}
    R={}
    RMS={}

    tic=time()
    # define the grids
    grids, bds = setup_grids(args)

    # select only the data points that are within the grid bounds
    valid_z0=grids['z0'].validate_pts((args['data'].coords()[0:2]))
    valid_dz=grids['dz'].validate_pts((args['data'].coords()))
    valid_data=valid_data & valid_dz & valid_z0

    if not np.any(valid_data):
        if args['VERBOSE']:
            print("smooth_xytb_fit_aug: no valid data")
        return {'m':m, 'E':E, 'data':None, 'grids':grids, 'valid_data': valid_data, 'TOC':{},'R':{}, 'RMS':{}, 'timing':timing,'E_RMS':args['E_RMS']}

    # subset the data based on the valid mask
    data=args['data'].copy_subset(valid_data)

    # if we have a mask file, use it to subset the data
    # needs to be done after the valid subset because otherwise the interp_mtx for the mask file fails.
    if args['mask_file'] is not None:
        setup_mask(data, grids, valid_data, bds, args)

    # Check if we have any data.  If not, quit
    if data.size==0:
        return {'m':m, 'E':E, 'data':data, 'grids':grids, 'valid_data': valid_data, 'TOC':{},'R':{}, 'RMS':{}, 'timing':timing,'E_RMS':args['E_RMS']}

    # define the interpolation operator, equal to the sum of the dz and z0 operators
    G_data=lin_op(grids['z0'], name='interp_z').interp_mtx(data.coords()[0:2])
    G_data.add(lin_op(grids['dz'], name='interp_dz').interp_mtx(data.coords()))

    # define the smoothness constraints
    constraint_op_list=[]
    setup_smoothness_constraints(grids, constraint_op_list, args['E_RMS'], args['mask_scale'])

    ### NB: NEED TO MAKE THIS WORK WITH SETUP_GRID_BIAS
    #if args['E_RMS_d2x_PS_bias'] is not None:
    #    setup_PS_bias(data, G_data, constraint_op_list, grids, bds, args)

    # if bias params are given, create a set of parameters to estimate them
    if args['bias_params'] is not None:
        data, bias_model = assign_bias_ID(data, args['bias_params'], \
                                          bias_filter=args['bias_filter'])
        setup_bias_fit(data, bias_model, G_data, constraint_op_list,
                       bias_param_name='bias_ID')
        if args['bias_nsigma_edit']:
            bias_model['bias_param_dict']['edited']=np.zeros_like(bias_model['bias_param_dict']['ID'], dtype=bool)

        if args['bias_edit_vals'] is not None:
            edit_bias_list=np.c_[[args['bias_edit_vals'][key] for key in args['bias_edit_vals'].keys()]].T.tolist()
            bias_list=np.c_[[bias_model['bias_param_dict'][key] for key in args['bias_edit_vals'].keys()]].T.tolist()
            for row in edit_bias_list:
                bias_model['bias_param_dict']['edited'][bias_list.index(row)]=True
            # apply the editing to the three_sigma_edit variable
            bad_IDs=[bias_model['bias_param_dict']['ID'][ii]
                     for ii in np.flatnonzero(bias_model['bias_param_dict']['edited'])]
            data.three_sigma_edit[np.in1d(data.bias_ID, bad_IDs)]=False
    else:
        bias_model={}
    if args['data_slope_sensors'] is not None and len(args['data_slope_sensors'])>0:
        #N.B.  This does not currently work.
        bias_model['E_slope']=args['E_slope']
        G_slope_bias, Gc_slope_bias, Cvals_slope_bias, bias_model = \
            data_slope_bias(data, bias_model, sensors=args['data_slope_sensors'],\
                            col_0=G_data.col_N)
        G_data.add(G_slope_bias)
        constraint_op_list.append(Gc_slope_bias)


        # setup priors
    if args['prior_args'] is not None:
        constraint_op_list += match_prior_dz(grids, **args['prior_args'])

    for op in constraint_op_list:
        if op.prior is None:
            op.prior=np.zeros_like(op.expected)

    # put the equations together
    Gc=lin_op(None, name='constraints').vstack(constraint_op_list)

    N_eq=G_data.N_eq+Gc.N_eq

    # put together all the errors
    Ec=np.zeros(Gc.N_eq)
    for op in constraint_op_list:
        try:
            Ec[Gc.TOC['rows'][op.name]]=op.expected
        except ValueError as E:
            print("smooth_xytb_fit_aug:\n\t\tproblem with "+op.name)
            raise(E)
    if args['data_slope_sensors'] is not None and len(args['data_slope_sensors']) > 0:
        Ec[Gc.TOC['rows'][Gc_slope_bias.name]] = Cvals_slope_bias
    Ed=data.sigma.ravel()
    if np.any(Ed==0):
        raise(ValueError('zero value found in data sigma'))
    if np.any(Ec==0):
        raise(ValueError('zero value found in constraint sigma'))
    #print({op.name:[Ec[Gc.TOC['rows'][op.name]].min(),  Ec[Gc.TOC['rows'][op.name]].max()] for op in constraint_op_list})
    # calculate the inverse square root of the data covariance matrix
    TCinv=sp.dia_matrix((1./np.concatenate((Ed, Ec)), 0), shape=(N_eq, N_eq))

    # define the right hand side of the equation
    rhs=np.zeros([N_eq])
    rhs[0:data.size]=data.z.ravel()
    rhs[data.size:]=np.concatenate([op.prior for op in constraint_op_list])

    # put the fit and constraint matrices together
    Gcoo=sp.vstack([G_data.toCSR(), Gc.toCSR()]).tocoo()

    # setup operators that take averages of the grid at different scales
    averaging_ops = setup_averaging_ops(grids['dz'], G_data.col_N, args)

    # setup masked averaging ops
    averaging_ops.update(setup_avg_mask_ops(grids['dz'], G_data.col_N, args['avg_masks'], args['dzdt_lags']))

    # define the matrix that sets dz[reference_epoch]=0 by removing columns from the solution:
    Ip_c = build_reference_epoch_matrix(G_data, Gc, grids, args['reference_epoch'])

    # eliminate the columns for the model variables that are set to zero
    Gcoo=Gcoo.dot(Ip_c)
    timing['setup']=time()-tic

    # initialize the book-keeping matrices for the inversion
    if "three_sigma_edit" in data.fields:
        in_TSE=data.three_sigma_edit > 0.01
    else:
        in_TSE=np.ones(G_data.N_eq, dtype=bool)

    if args['VERBOSE']:
        print("initial: %d:" % G_data.r.max(), flush=True)

    # if we've done any iterations, parse the model and the data residuals
    if args['max_iterations'] > 0:
        tic_iteration=time()
        m0, sigma_extra, in_TSE, rs_data=iterate_fit(data, Gcoo, rhs, \
                                TCinv, G_data, Gc, in_TSE, Ip_c, timing, args,
                                bias_model=bias_model)

        timing['iteration']=time()-tic_iteration
        valid_data[valid_data]=in_TSE
        data.assign({'three_sigma_edit':in_TSE})

        # report the model-based estimate of the data points
        data.assign({'z_est':np.reshape(G_data.toCSR().dot(m0), data.shape)})
        parse_model(m, m0, data, R, RMS, G_data, averaging_ops, Gc, Ec, grids, bias_model, args)
        r_data=data.z_est[data.three_sigma_edit==1]-data.z[data.three_sigma_edit==1]
        R['data']=np.sum(((r_data/data.sigma[data.three_sigma_edit==1])**2))
        RMS['data']=np.sqrt(np.mean((data.z_est[data.three_sigma_edit==1]-data.z[data.three_sigma_edit==1])**2))

    # Compute the error in the solution if requested
    if args['compute_E']:
        r_data=data.z_est[data.three_sigma_edit==1]-data.z[data.three_sigma_edit==1]
        sigma_extra=calc_sigma_extra(r_data, data.sigma[data.three_sigma_edit==1])

        # rebuild TCinv to take into account the extra error
        TCinv=sp.dia_matrix((1./np.concatenate((np.sqrt(Ed**2+sigma_extra**2), Ec)), 0), shape=(N_eq, N_eq))

        # We have generally not done any iterations at this point, so need to make the Ip_r matrix
        cov_rows=G_data.N_eq+np.arange(Gc.N_eq)
        Ip_r=sp.coo_matrix((np.ones(Gc.N_eq+in_TSE.sum()), \
                           (np.arange(Gc.N_eq+in_TSE.sum()), \
                            np.concatenate((np.flatnonzero(in_TSE), cov_rows)))), \
                           shape=(Gc.N_eq+in_TSE.sum(), Gcoo.shape[0])).tocsc()
        if args['VERBOSE']:
            print("Starting uncertainty calculation", flush=True)
            tic_error=time()
        calc_and_parse_errors(E, Gcoo, TCinv, rhs, Ip_c, Ip_r, grids, G_data, Gc, averaging_ops, \
                         bias_model, args['bias_params'], dzdt_lags=args['dzdt_lags'], timing=timing, \
                             error_res_scale=args['error_res_scale'])
        if args['VERBOSE']:
            print("\tUncertainty propagation took %3.2f seconds" % (time()-tic_error), flush=True)

    TOC=Gc.TOC
    return {'m':m, 'E':E, 'data':data, 'grids':grids, 'valid_data': valid_data, \
            'TOC':TOC,'R':R, 'RMS':RMS, 'timing':timing,'E_RMS':args['E_RMS'], \
                'dzdt_lags':args['dzdt_lags']}

def main():
    W={'x':1.e4,'y':200,'t':2}
    x=np.arange(-W['x']/2, W['x']/2, 100)
    D=pc.data().from_dict({'x':x, 'y':np.zeros_like(x),'z':np.sin(2*np.pi*x/2000.),\
                           'time':np.zeros_like(x)-0.5, 'sigma':np.zeros_like(x)+0.1})
    D1=D
    D1.t=np.ones_like(x)
    data=pc.data().from_list([D, D.copy().assign({'time':np.zeros_like(x)+0.5})])
    E_d3zdx2dt=0.0001
    E_d2z0dx2=0.006
    E_d2zdt2=5
    E_RMS={'d2z0_dx2':E_d2z0dx2, 'dz0_dx':E_d2z0dx2*1000, 'd3z_dx2dt':E_d3zdx2dt, 'd2z_dxdt':E_d3zdx2dt*1000,  'd2z_dt2':E_d2zdt2}

    ctr={'x':0., 'y':0., 't':0.}
    SRS_proj4='+proj=stere +lat_0=90 +lat_ts=70 +lon_0=-45 +k=1 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs '
    spacing={'z0':50, 'dz':50, 'dt':0.25}

    S=smooth_xytb_fit_aug(data=data, ctr=ctr, W=W, spacing=spacing, E_RMS=E_RMS,
                     reference_epoch=2, N_subset=None, compute_E=False,
                     max_iterations=2,
                     srs_proj4=SRS_proj4, VERBOSE=True, dzdt_lags=[1])
    return S


if __name__=='__main__':
    main()
