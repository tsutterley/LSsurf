#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Mon Dec 30 14:22:56 2019

@author: ben
"""

import pointCollection as pc
import numpy as np
import json
from pathlib import Path
import os
import matplotlib.pyplot as plt

def read_swath_data(xy0, W, index_file, apply_filters=True, dem_file=None, dem_tol=10):
    fields = ['x','y','time','h', 'power','coherence','AD','error_composite',\
              'R_POCA', 'ambiguity','burst','abs_orbit', 'block_h_spread',\
              'count','phase','R_offnadir','range_surf','seg_ind',\
              'delta_roll', 'pulse_num']
        
    D=pc.geoIndex().from_file(index_file).query_xy_box(xy0[0]+np.array([-W/2, W/2]), \
                   xy0[1]+np.array([-W/2, W/2]), fields=fields)
    D=pc.data().from_list(D)
    if D is None:
        return D
    D.assign({'burst':D.pulse_num, 'swath':np.ones_like(D.x, dtype=bool)})

    if apply_filters:
        if np.any(D.count>1):
            D.index( (D.power > 1e-17) & (D.power < 1e-13) & (D.error_composite==0) & \
                    (D.count > 3) & (D.block_h_spread < 15))
        else:
            D.index( (D.power > 1e-17) & (D.power < 1e-13) & (D.error_composite==0))
    return D

def read_poca_data(xy0, W, index_file, apply_filters=True, dem_file=None, dem_tol=10):
    fields=['x','y','time','h', 'power','coherence','AD','error_composite', \
            'ambiguity','pulse_num','abs_orbit','phase','range_surf']
    D=pc.geoIndex().from_file(index_file).query_xy_box(xy0[0]+np.array([-W/2, W/2]), \
                   xy0[1]+np.array([-W/2, W/2]), fields=fields)
    D=pc.data().from_list(D)
    if D is None:
        return D
    D.assign({'burst':D.pulse_num,  'swath':np.zeros_like(D.x, dtype=bool)})

    if apply_filters:
        D.index((D.power > 1e-16) & (D.error_composite == 0) & (D.power < 1e-12))
    return D

def read_cs2_data(xy0, W, index_files, apply_filters=True, dem_file=None, dem_tol=10):
    D=pc.data().from_list([\
            read_poca_data( xy0, W, index_files['POCA'], apply_filters=apply_filters), 
            read_swath_data(xy0, W, index_files['swath'], apply_filters=apply_filters)])
    
    if dem_file is not None:
        z_DEM=pc.grid().from_file(dem_file, bounds=[xy0[0]+np.array([-W/2, W/2]), \
                                    xy0[1]+np.array([-W/2, W/2])]).interp(D.x, D.y)
        D.index(np.abs(D.h-z_DEM) < dem_tol)
    
    if apply_filters:
        with open(os.path.join(Path(__file__).parent.absolute(), \
                     'CS2_known_bad_orbits.json'), 'r') as fh:
            bad_orbits=json.load(fh)[1]#['bad orbits']
        D.index( ~np.in1d(D.abs_orbit.astype(int), np.array(bad_orbits, dtype='int')))
    return D

def test():
    index_files={'POCA':'/Volumes/insar6/ben/Cryosat/POCA_h5_C/AA_REMA_v1_sigma4/GeoIndex.h5', \
                 'swath':'/Volumes/insar6/ben/Cryosat/SW_h5_C/AA_REMA_v1_sigma4/GeoIndex.h5'}
    D=read_cs2_data([-1400000.0, -450000], 4.1e4, index_files)
    fig=plt.figure(); 
    fig.add_subplot(2, 1, 1)
    plt.hist2d(D.x[D.swath==0], D.y[D.swath==0], 25)
    plt.axis('equal');
    plt.colorbar()
    fig.add_subplot(2, 1, 2)
    plt.hist2d(D.x[D.swath==1], D.y[D.swath==1], 25)
    plt.axis('equal');
    plt.colorbar()
    plt.show()
    return D
    
if __name__=='__main__':
    D=test()
    