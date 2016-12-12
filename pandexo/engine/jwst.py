import sys
import json
import numpy as np
import pandas as pd
from copy import deepcopy 

from pandeia.engine.instrument_factory import InstrumentFactory
from pandeia.engine.perform_calculation import perform_calculation
import create_input as create
from compute_noise import ExtractSpec

#constant parameters.. consider putting these into json file 
#max groups in integration
max_ngroup = 65536.0 
#minimum number of integrations
min_nint_trans = 1

def compute_full_sim(dictinput): 
    """
    Function to set up explanet observations for JWST only and 
    compute simulated spectrum. It uses STScI's Pandeia to compute 
    instrument throughputs and WebbPSF to compute PSFs. 
    
    :param dictinput: dictionary containing instrument parameters and exoplanet specific parameters. {"pandeia_input":dict1, "pandexo_input":dict1}
    :type inputdict: dict
    :returns: large dictionary with 1d, 2d simualtions, timing info, instrument info, warnings
    :rtype: dict
    
    :Example:
    
    >>> from pandexo.engine.jwst import compute_full_sim 
    >>> from pandexo.engine.justplotit import jwst_1d_spec
    >>> a = compute_full_sim({"pandeia_input": pandeiadict, "pandexo_input":exodict})
    >>> jwst_1d_spec(a)
    .. image:: 1d_spec.png
    
    .. note:: It is much easier to run simulations through either **run_online** or **justdoit**. **justdoit** contains functions to create input dictionaries and **run_online** contains web forms to create input dictionaries.
    .. seealso:: 
        Module :justdoit:`justdoit`
            Documentation of the :mod:`justdoit` 
        Module :run_online:`run_online`
            Documentation of the :mod:`run_online`
    """
    pandeia_input = dictinput['pandeia_input']
    pandexo_input = dictinput['pandexo_input']    	
	
    #define the calculation we'll be doing 
    if pandexo_input['planet']['w_unit'] == 'sec':
        calculation = 'phase_spec'
    else: 
        calculation = pandexo_input['calculation'].lower()

    #which instrument 
    instrument = pandeia_input['configuration']['instrument']['instrument']
    conf = {'instrument': pandeia_input['configuration']['instrument']}
    i = InstrumentFactory(config=conf)
    det_pars = i.get_detector_pars()
    fullwell = det_pars['fullwell']
    rn = det_pars['rn']
    pix_size = det_pars['pix_size']*1e-3 #convert from miliarcsec to arcsec
    sat_level = pandexo_input['observation']['sat_level']/100.0*fullwell

    #parameteres needed from exo_input
    mag = pandexo_input['star']['mag']
    
    
    noccultations = pandexo_input['observation']['noccultations']
    R = pandexo_input['observation']['R']
    #amount of exposure time out-of-occultation, as a fraction of in-occ time 
    expfact_out = pandexo_input['observation']['fraction'] 
    noise_floor = pandexo_input['observation']['noise_floor']


    #get stellar spectrum and in transit spec
    star_spec = create.outTrans(pandexo_input['star'])
    both_spec = create.bothTrans(star_spec, pandexo_input['planet'])
    out_spectrum = np.array([both_spec['wave'], both_spec['flux_out_trans']])
        
    #get transit duration from phase curve or from input 
    if calculation == 'phase_spec': 
        transit_duration = max(both_spec['time']) - min(both_spec['time'])
    else: 
        transit_duration = pandexo_input['planet']['transit_duration']

    #add to pandeia input 
    pandeia_input['scene'][0]['spectrum']['sed']['spectrum'] = out_spectrum
    
    #run pandeia once to determine max exposure time per int and get exposure params
    print "Computing Duty Cycle"
    m = compute_maxexptime_per_int(pandeia_input, sat_level) 
    print "Finished Duty Cucle Calc"

    #calculate all timing info
    timing, flags = compute_timing(m,transit_duration,expfact_out,noccultations)
    
    #Simulate out trans and in transit
    print "Starting Out of Transit Simulation"
    out = perform_out(pandeia_input, pandexo_input,timing, both_spec)
    print "End out of Transit"

    #this kind of redundant going to compute inn from out instead 
    #keep perform_in but change inputs to (out, timing, both_spec)
    print "Starting In Transit Simulation"
    inn = perform_in(pandeia_input, pandexo_input,timing, both_spec, out, calculation)
    print "End In Transit" 

    #compute warning flags for timing info 
    warnings = add_warnings(out, timing, sat_level, flags, instrument) 

    compNoise = ExtractSpec(inn, out, rn, pix_size, timing)
    
    #slope method is pandeia's pure noise calculation (taken from SNR)
    #contains correlated noise, RN, dark current, sky, 
    #uses MULTIACCUM formula so we deviated from this. 
    #could eventually come back to this if Pandeia adopts First-Last formula
    if calculation == 'slope method': 
        #Extract relevant info from pandeia output (1d curves and wavelength) 
        #extracted flux in units of electron/s
        w = out['1d']['extracted_flux'][0]
        result = compNoise.run_slope_method()

    #derives noise from 2d postage stamps. Doing this results in a higher 
    #1d flux rate than the Pandeia gets from extracting its own. 
    #this should be used to benchmark Pandeia's 1d extraction  
    elif calculation == '2d extract':
        w = out['1d']['extracted_flux'][0]
        result = compNoise.run_2d_extract()
    
    #this is the noise calculation that PandExo uses online. It derives 
    #its own calculation of readnoise and does not use MULTIACUMM 
    #noise formula  
    elif calculation == 'fml':
        w = out['1d']['extracted_flux'][0]
        result = compNoise.run_f_minus_l()
    
    elif calculation == 'phase_spec':
        result = compNoise.run_phase_spec()
        w = result['time']
    else:
        result = None
        raise Exception('WARNING: Calculation method not found.')
        
    varin = result['var_in_1d']
    varout = result['var_out_1d']
    extracted_flux_out = result['photon_out_1d']
    extracted_flux_inn = result['photon_in_1d']

        
    #bin the data according to user input 
    if R != None: 
        wbin = bin_wave_to_R(w, R)
        photon_out_bin = uniform_tophat_sum(wbin, w,extracted_flux_out)
        photon_in_bin = uniform_tophat_sum(wbin,w, extracted_flux_inn)
        var_in_bin = uniform_tophat_sum(wbin, w,varin)
        var_out_bin = uniform_tophat_sum(wbin,w, varout)
    else: 
        wbin = w
        photon_out_bin = extracted_flux_out
        photon_in_bin = extracted_flux_inn
        var_in_bin = varin
        var_out_bin = varout
    
    #calculate total variance
    var_tot = var_in_bin + var_out_bin
    error = np.sqrt(var_tot)
    
    #calculate error on spectrum
    error_spec = error/photon_out_bin
   
    #Add in user specified noise floor 
    error_spec_nfloor = add_noise_floor(noise_floor, wbin, error_spec) 

    
    #add in random noise for the simulated spectrum 
    rand_noise= np.sqrt((var_in_bin+var_out_bin))*(np.random.randn(len(wbin)))
    raw_spec = (photon_out_bin-photon_in_bin)/photon_out_bin
    sim_spec = (photon_out_bin-photon_in_bin + rand_noise)/photon_out_bin 
    
    #if secondary tranist, multiply spectra by -1 
    if pandexo_input['planet']['f_unit'] == 'fp/f*':
        sim_spec = -1.0*sim_spec
        raw_spec = -1.0*raw_spec
    
   
    #package processed data
    binned = {'wave':wbin,
              'spectrum': raw_spec,
              'spectrum_w_rand':sim_spec,
              'error_w_floor':error_spec_nfloor}
    
    unbinned = {
                'flux_out':extracted_flux_out, 
                'flux_in':extracted_flux_inn,
                'var_in':varin, 
                'var_out':varout, 
                'wave':w,
                'error_no_floor':np.sqrt(varin+varout)/extracted_flux_out
                }
 
    result_dict = as_dict(out,both_spec ,binned, 
                timing, mag, sat_level, warnings,
                pandexo_input['planet']['f_unit'], unbinned,calculation)

    return result_dict 
    
def compute_maxexptime_per_int(pandeia_input, sat_level):
    """
    Function to simulate 2d jwst image with 2 groups, 1 integration, 1 exposure 
    and return the maximum time 
    for one integration before saturation occurs. If saturation has 
    already occured, returns maxexptime_per_int as np.nan. This then 
    tells Pandexo to set min number of groups (ngroups =2). This avoids 
    error if saturation occurs. This routine assumes that min ngroups is 2. 
    
    :param pandeia_input: pandeia dictionary input 
    :param sat_level: user defined saturation level in units of electrons
    :type pandeia_input: dict
    :type sat_level: int, float... 
    :returns: dict with maximum exposure time per integration, nframes, nskip, frame time
    :rtype: dict
    
    :Example: 
    
    >>> from pandexo.engine.jwst import compute_maxexptime_per_int as cmpi 
    >>> max_time = cmpi(pandeia_input, 50000.0)
    >>> print max_time 
    {'maxexptime_per_int':12.0, 'nframe':1, 'nskip':0, 'exptime_per_frame': 0.55}
    """
    
    #run once to get 2d rate image 
    pandeia_input['configuration']['detector']['ngroup'] = 2 
    pandeia_input['configuration']['detector']['nint'] = 1 
    pandeia_input['configuration']['detector']['nexp'] = 1
    
    report = perform_calculation(pandeia_input, dict_report=False)
    report_dict = report.as_dict() 

            
    #check for hard saturation 
    if 'saturated' in report_dict['warnings'].keys(): 
        if report_dict['warnings']['saturated'][0:4] == 'Hard':
            print('Hard saturation with minimum number of groups')
    
    # count rate on the detector in e-/second 
    det = report_dict['2d']['detector']
    
    timeinfo = report_dict['information']['exposure_specification']
    #totaltime = timeinfo['tgroup']*timeinfo['ngroup']*timeinfo['nint']
    
    maxdetvalue = np.max(det)
    #maximum time before saturation per integration 
    #based on user specified saturation level
    try:
        maxexptime_per_int = sat_level/maxdetvalue
    except: 
        maxexptime_per_int = np.nan
        
    exptime_per_frame = report_dict['information']['exposure_specification']['tframe']
    nframe = report_dict['information']['exposure_specification']['nframe']
    nskip = report_dict['information']['exposure_specification']['nskip']
    return {'maxexptime_per_int':maxexptime_per_int, 'nframe':nframe, 'nskip':nskip, 'exptime_per_frame': exptime_per_frame}
        
def compute_timing(m,transit_duration,expfact_out,noccultations): 
    """
    Computes all JWST specific timing info for observation including. Some pertinent 
    JWST terminology:

        - frame: The result of sequentially clocking and digitizing all pixels in a rectangular area of an SCA. **Full-fame readout** means to digitize all pixels in an SCA, including reference pixels. **Frame** also applies to the result of clocking and digitizing a subarray on an SCA.
        - group: One or more consecutively read frames. There are no intervening resets. Frames may be averaged to form a group but for exoplanets the read out scheme is always 1 frame = 1 group
        - integration: The end result of resetting the detector and then non-destructively sampling it one or more times over a finite period of time before resetting the detector again. This is a unit of data for which signal is proportional to intensity, and it consists of one or more GROUPS.
        - exposure: The end result of one or more INTEGRATIONS over a finite period of time.  EXPOSURE defines the contents of a single FITS file.
    
    :param m: dictionary output from **compute_maxexptime_per_int**
    :param transit_duration: transit duration in seconds 
    :param expfact_out: fraction of time spent in transit versus out of transit 
    :param noccultations: number of transits 
    :type m: dict
    :type transit_duration: int, float...
    :type expfact_out: int, float...
    :type noccultations: int, float... 
    :returns: timing-- dict with all timing info, warningflags--dict with two warning flags
    :rtype: 2 dictionaries
    
    :Example:
    
    >>> from pandexo.engine.jwst import compute_timing, 
    >>> timing, flags = compute_timing(m, 2*60.0*60.0, 1.0, 1.0)
    >>> print timing.keys()
    ['Number of Transits', 'Num Integrations Out of Transit', 'Num Integrations In Transit', 'Num Groups per Integration', 'Seconds per Frame', 'Observing Efficiency (%)', 'On Source Time(sec)', 'Exposure Time Per Integration (secs)', 'Reset time Plus 30 min TA time (hrs)', 'Num Integrations per Occultation', 'Transit Duration']
    """
    exptime_per_frame = m['exptime_per_frame']
    nframe = m['nframe']
    nskip = m['nskip']
    overhead_per_int = exptime_per_frame #overhead time added per integration 
    maxexptime_per_int = m['maxexptime_per_int']

    flag_default = "All good"
    flag_high = "All good"
    try:
        #number of frames in one integration is the maximum time beofre exposure 
        #divided by the time it takes for one frame. Note this does not include 
        #reset frames 

        nframes_per_int = long(maxexptime_per_int/exptime_per_frame)
    
        #for exoplanets nframe =1 an nskip always = 1 so ngroups_per_int 
        #and nframes_per_int area always the same 
        ngroups_per_int = long(nframes_per_int/(nframe + nskip)) 
    
        #put restriction on number of groups 
        #there is a hard limit to the maximum number groups. 
        #if you exceed that limit, set it to the maximum value instead.
        #also set another check for saturation
    
        if ngroups_per_int > max_ngroup:
            ngroups_per_int = max_ngroup
            print("Num of groups per int exceeded max num of allowed groups"+str(ngroups_per_int))
            print("Setting number of groups to max value = 65536.0")
            flag_high = "Groups/int > max num of allowed groups"
 
        if ngroups_per_int < 2:
            ngroups_per_int = 2.0  
            nframes_per_int = 2
            print("Hard saturation during first group. Check Pandeia Warnings.")
            flag_default = "NGROUPS<2, SET TO NGROUPS=2 BY DEFAULT"
    except: 
        #if maxexptime_per_int is nan then just ngroups and nframe to 2 
        #for the sake of not returning error
        nframes_per_int = 2
        ngroups_per_int = 2
        flag_default = "NGROUPS<2, SET TO NGROUPS=2 BY DEFAULT"
                
    #the integration time is related to the number of groups and the time of each 
    #group 
    exptime_per_int = ngroups_per_int*exptime_per_frame
    
    #clock time includes the reset frame 
    clocktime_per_int = ngroups_per_int*exptime_per_frame
    
    #observing efficiency (i.e. what percentage of total time is spent on soure)
    eff = (ngroups_per_int - 1.0)/(ngroups_per_int + 1.0)
    
    #this says "per occultation" but this is just the in transit frames.. See below
    #nframes_per_occultation = long(transit_duration/exptime_per_frame)
    #ngroups_per_occultation = long(nframes_per_occultation/(nframe + nskip))
    nint_per_occultation =  transit_duration*eff/exptime_per_int
    
    #figure out how many integrations are in transit and how many are out of transit 
    nint_in = np.ceil(nint_per_occultation)
    nint_out = np.ceil(nint_in/expfact_out)
    
    #you would never want a single integration in transit. 
    #here we assume that for very dim things, you would want at least 
    #3 integrations in transit 
    if nint_in < min_nint_trans:
        ngroups_per_int = np.floor(ngroups_per_int/3.0)
        exptime_per_int = (ngroups_per_int-1.)*exptime_per_frame
        clocktime_per_int = ngroups_per_int*exptime_per_frame
        eff = (ngroups_per_int - 1.0)/(ngroups_per_int + 1.0)
        nint_per_occultation =  transit_duration*eff/exptime_per_int
        nint_in = np.ceil(nint_per_occultation)
        nint_out = np.ceil(nint_in/expfact_out)
        
    if nint_out < min_nint_trans:
        nint_out = min_nint_trans
   
    timing = {
        "Transit Duration" : transit_duration/60.0/60.0,
        "Seconds per Frame" : exptime_per_frame,
        "Exposure Time Per Integration (secs)":exptime_per_int,
        "Num Groups per Integration" :ngroups_per_int, 
        "Num Integrations Out of Transit":nint_out,
        "Num Integrations In Transit":nint_in,
        "Num Integrations per Occultation":nint_out+nint_in,
        "On Source Time(sec)": noccultations*clocktime_per_int*(nint_out+nint_in),
        "Reset time Plus 30 min TA time (hrs)": overhead_per_int*(nint_in + nint_out)/60.0/60.0 + 0.5,
        "Observing Efficiency (%)": eff*100.0,
        "Number of Transits": noccultations
        }      
        
    return timing, {'flag_default':flag_default,'flag_high':flag_high}

def perform_out(pandeia_input, pandexo_input,timing, both_spec):
    """
    Runs pandeia for the out of transit data
    
    :param pandeia_input: pandeia specific input info 
    :param pandexo_input: exoplanet specific observation info 
    :param timing: timing dictionary from **compute_timing** 
    :param both_spec: dictionary transit spectra computed from **createInput.bothTrans** 
    :type pandeia_input: dict
    :type pandexo_input: dict
    :type timing: dict
    :type both_spec: dict
    :returns: report_out--pandeia output dictionary 
    :rtype: dict
    """
    #pandeia inputs, simulate one integration at a time 
    pandeia_input['configuration']['detector']['ngroup'] = timing['Num Groups per Integration']
    pandeia_input['configuration']['detector']['nint'] = timing['Num Integrations Out of Transit']
    pandeia_input['configuration']['detector']['nexp'] = 1 

    report_out = perform_calculation(pandeia_input, dict_report=True)
    report_out.pop('3d')

    return report_out

    
def perform_in(pandeia_input, pandexo_input,timing, both_spec, out, calculation): 
    """
    Runs Pandeia for the in transit data or computes the in transit simulation 
    from the out of transit pandeia run 
    
    :param pandeia_input: pandeia specific input info 
    :param pandexo_input: exoplanet specific observation info 
    :param timing: timing dictionary from **compute_timing** 
    :param both_spec: dictionary transit spectra computed from **createInput.bothTrans** 
    :param out: out of transit dictionary from **perform_in*
    :param calculation: key which speficies the kind of noise calcualtion (2d extract, slope method, fml, phase_spec). Recommended for transit transmisstion spectra = fml
    :type pandeia_input: dict
    :type pandexo_input: dict
    :type timing: dict
    :type both_spec: dict
    :type out: dict
    :type calculation: str
    :returns: report_in--pandeia output dictionary 
    :rtype: dict
    """
    
    #function to run pandeia for in transit
    if calculation == 'phase_spec':
        #return the phase curve since it's all we need 
        report_in = {'time': both_spec['time'],'planet_phase': both_spec['planet_phase']}
    elif calculation == 'fml':
        #for FML method, we only use the flux rate calculated in pandeia so 
        #can compute in transit flux rate without running pandeia a third time     
        report_in = deepcopy(out)
        
        transit_depth = np.interp(report_in['1d']['extracted_flux'][0],
                                    both_spec['wave'], both_spec['frac'])
        report_in['1d']['extracted_flux'][1] = report_in['1d']['extracted_flux'][1]*transit_depth
    else: 
        #only run pandeia a third time if doing slope method and need accurate run for the 
        #nint and timing
        pandeia_input['configuration']['detector']['ngroup'] = timing['Num Groups per Integration']
        pandeia_input['configuration']['detector']['nint'] = timing['Num Integrations In Transit']
        pandeia_input['configuration']['detector']['nexp'] = 1
  
        in_transit_spec = np.array([both_spec['wave'], both_spec['flux_in_trans']])
    
        pandeia_input['scene'][0]['spectrum']['sed']['spectrum'] = in_transit_spec

        report_in = perform_calculation(pandeia_input, dict_report=True)
        report_in.pop('3d')
    
    return report_in
          
def add_warnings(pand_dict, timing, sat_level, flags,instrument): 
    """
    Adds in necessary warning flags for a JWST observation usually associated with 
    too few or too many groups or saturation. Alerts user if saturation level is higher 
    than 80 percent and if the number of groups is less than 5. Or, if the full well is 
    greater than 80. These warnings are currently very arbitrary. Will be updated as 
    better JWST recommendations are made. 
    
    :param pand_dict: output from pandeia run 
    :param timing: output from **compute_timing** 
    :param sat_level: user specified saturation level in electrons 
    :param flags: warning flags taken from output of **compute_timing**
    :param instrument: allowed: nirspec, niriss, nircam, miri
    :type pand_dict: dict
    :type timing: dict
    :type sat_level: int, float... 
    :type flags: dict
    :type instrument: str
    :returns: warnings
    :rtype: dict
    
    .. note:: These are warnings are just suggestions and are not yet required. 
    .. todo:: Update as new requirements become available 
    """

    ngroups_per_int = timing['Num Groups per Integration']
  
    #check for saturation 
    try:  
        flag_nonl = pand_dict['warnings']['nonlinear']
    except: 
        flag_nonl = "All good"    
    try: 
        flag_sat = pand_dict['warnings']['saturated']
    except: 
        flag_sat = "All good"
        
    #check for too small number of groups
    flag_low = "All good"
    flag_perc = "All good"

    if (sat_level > 80) & (ngroups_per_int <5):
        flag_low = "% full well>80% & only " + str(ngroups_per_int) + " groups"
    if (sat_level > 80): 
        flag_perc = "% full well>80%"

     
    warnings = {
            "Group Number Too Low?" : flag_low,
            "Group Number Too High?": flags["flag_high"],
            "Non linear?" : flag_nonl,
            "Saturated?" : flag_sat,
            "% full well high?": flag_perc, 
            "Num Groups Reset?": flags["flag_default"]
    }

    return warnings     
    
def add_noise_floor(noise_floor, wave_bin, error_spec):
    """
    This adds in a user speficied noise floor. Does not add the noise floor in quadrature 
    isntead it sets error[error<noise_floor] = noise_floor. If a wavelength dependent 
    noise floor is given and the wavelength ranges are off, it interpolates the out of 
    range noise floor. 
    
    :param noise_floor: file with two column [wavelength, noise(ppm)] or single number with constant noise floor in ppm 
    :param wave_bin: final binned wavelength grid from simulation 
    :param error_spec: final computed error on the planet spectrum in units of rp^2/r*^2 or fp/f*
    :type noise_floor: str or int, float...
    :type wave_bin: array of floats
    :type error_spec: array of floats
    :returns: error_spec-- new error
    :rtype: array of floats
    
    :Example:
    
    >>> from pandexo.engine.jwst import add_noise_floor
    >>> import numpy as np
    >>> wave = np.linspace(1,2.7,10)
    >>> error = np.zeros(10)+1e-6
    >>> newerror = add_noise_floor(20, wave, error)
    >>> print newerror
    [  2.00000000e-05   2.00000000e-05   2.00000000e-05   2.00000000e-05
       2.00000000e-05   2.00000000e-05   2.00000000e-05   2.00000000e-05
       2.00000000e-05   2.00000000e-05]
    """
    #add user specified noise floor 
    if (type(noise_floor)==float) | (type(noise_floor) == int):
        error_spec[error_spec<noise_floor*1e-6] = noise_floor*1e-6
    elif (type(noise_floor)==str):
        read_noise = np.genfromtxt(noise_floor, dtype=(float, float), names='w, n')
        w_overlap = (wave_bin>=min(read_noise['w'])) & (wave_bin<=max(read_noise['w'])) 
        wnoise = wave_bin[w_overlap]
        noise = np.zeros(len(wave_bin))
        noise[w_overlap] = np.interp(wnoise , read_noise['w'], read_noise['n'])
        noise[(wave_bin>max(read_noise['w']))] = read_noise['n'][read_noise['w'] == max(read_noise['w'])]
        noise[(wave_bin<min(read_noise['w']))] = read_noise['n'][read_noise['w'] == min(read_noise['w'])]
        error_spec[error_spec<noise*1e-6] = noise[error_spec<noise*1e-6]*1e-6
    else: 
        raise ValueError('Noise Floor added was not integer or file')
    return error_spec

def bin_wave_to_R(w, R):
    """
    Given a wavelength that is at the instrinsic resolution of the instrument 
    bin to a new wavelength of resolution, R
    
    :param w: old wavelength axis at high-er resolution 
    :param R: resolution of the desired new wavelength 
    :type w: array of float 
    :type R: int, float...
    :returns: wave--new wavelength grid 
    :rtype: array of float
    
    :Example:
    
    >>> from pandexo.engine.jwst import bin_wave_to_R
    >>> newwave = bin_wave_to_R(np.linspace(1,2,1000), 10)
    >>> print len(newwave)
    11
    """
    wave = []
    tracker = min(w)
    i = 1 
    ind= 0
    while(tracker<max(w)):
        if i <len(w)-1:
        
            dlambda = w[i]-w[ind]
            newR = w[i]/dlambda
            if newR < R:
                tracker = w[ind]+dlambda/2.0
                wave +=[tracker]
                ind = (np.abs(w-tracker)).argmin()
                i = ind
            else:            
                i+=1    
        else:
            tracker = max(w)
    return wave

def uniform_tophat_sum(newgrid,oldgrid, y):
    """
    Given a new grid this routine sums up pixels in unifrom top have sum (not mean). 
    Used for summing total photons in given bin. 
    Adapted from Mike R. Line

    :param newgrid: new wavelength grid 
    :param oldgrid: old wavelength grid at smaller resolution than new 
    :param y: photons you wish to rebin
    :type newgrid: array of float 
    :type oldgrid: array of float 
    :type y: array of float 
    :returns: newy--new y on new grid 
    :type: array of float
    
    :Example:
    
    >>> from pandexo.engine.jwst import uniform_tophat_sum
    >>> oldgrid = np.linspace(1,3,100)
    >>> y = np.zeros(100)+10.0
    >>> newy = uniform_tophat_sum(np.linspace(2,3,3), oldgrid, y)
    >>> newy
    array([ 240.,  250.,  130.])
    """
    newgrid = np.array(newgrid)
    szmod=newgrid.shape[0]
    delta=np.zeros(szmod)
    newy=np.zeros(szmod)
    delta[0:-1]=newgrid[1:]-newgrid[:-1]  
    delta[szmod-1]=delta[szmod-2] 
    #pdb.set_trace()
    for i in range(szmod-1):
        i=i+1
        loc=np.where((oldgrid >= newgrid[i]-0.5*delta[i-1]) & (oldgrid < newgrid[i]+0.5*delta[i]))
        newy[i]=np.sum(y[loc])
	    
    loc=np.where((oldgrid > newgrid[0]-0.5*delta[0]) & (oldgrid < newgrid[0]+0.5*delta[0]))
    newy[0]=np.sum(y[loc])
    return newy
    
def as_dict(out, both_spec ,binned, timing, mag, sat_level, warnings, punit, unbinned,calculation): 
    """
    Takes all output from jwst run and converts it to simple dictionary 
    
    :param out: output dictionary from **compute_out**
    :param both_spec: output dictionary from **createInput.bothTrans**
    :param binned: dictionary from **wrapper** 
    :param timing: dictionary from **compute_timing**
    :param mag: magnitude of system 
    :param sat_level: saturation level in electrons 
    :param warnings: warning dictionary from **add_warnings**
    :param punit: unit of supplied spectra options are: fp/f* or rp^2/r*^2
    :param unbinned: unbinned raw data from **wrapper**
    :param calculation: noise calculation type 
    :type out: dict
    :type both_spec: dict
    :type binned: dict
    :type timing: dict
    :type mag: float, int...
    :type sat_level: float, int...
    :type warnings: dict
    :type punit: str
    :type unbinned: dict
    :type calculation: str
    :returns: final_dict--compressed dictionary 
    :rtype: dict
    
    .. note:: Wouldn't advise running this routine outside of the justdoit/run_online/wrapper framework. 
    """
    #for emission spectrum

    p=1.0
    if punit == 'fp/f*': p = -1.0
    
    timing_div = pd.DataFrame(timing.items(), columns=['Timing Info', 'Values']).to_html().encode()
    timing_div = '<table class="table table-striped"> \n' + timing_div[36:len(timing_div)]
    
    warnings_div = pd.DataFrame(warnings.items(), columns=['Check', 'Status']).to_html().encode()
    warnings_div = '<table class="table table-striped"> \n' + warnings_div[36:len(warnings_div)]
       
    input_dict = {
   	 "Target Mag": mag , 
   	 "Saturation Level (electons)": sat_level, 
   	 "Instrument": out['input']['configuration']['instrument']['instrument'], 
   	 "Mode": out['input']['configuration']['instrument']['mode'], 
   	 "Aperture": out['input']['configuration']['instrument']['aperture'], 
   	 "Disperser": out['input']['configuration']['instrument']['disperser'], 
   	 "Subarray": out['input']['configuration']['detector']['subarray'], 
   	 "Readmode": out['input']['configuration']['detector']['readmode'], 
 	 "Filter": out['input']['configuration']['instrument']['filter'],
 	 "Primary/Secondary": punit
    }
    
    input_div = pd.DataFrame(input_dict.items(), columns=['Component', 'Values']).to_html().encode()
    input_div = '<table class="table table-striped"> \n' + input_div[36:len(input_div)]
    
    #add calc type to input dict (doing it here so it doesn't output on webpage
    input_dict["Calculation Type"]= calculation
    
    final_dict = {
    'OriginalInput': {'model_spec':both_spec['model_spec'],
                     'model_wave' : both_spec['model_wave']},
    'RawData': unbinned,
    'FinalSpectrum': binned,
        
    #pic output 
    'PandeiaOutTrans': out, 

    #all timing info 
    'timing': timing,
    'warning':warnings,
    'input':input_dict,
    
    #divs for html rendering    
    'timing_div':timing_div, 
    'input_div':input_div,
    'warnings_div':warnings_div,
    
    }
    return final_dict

    