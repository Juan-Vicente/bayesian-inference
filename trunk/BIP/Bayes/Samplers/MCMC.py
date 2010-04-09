# To change this template, choose Tools | Templates
# and open the template in the editor.
"""
Module implementing MCMC samplers 

    - Metropolis: Standard random Walk Metropolis Hastings sampler
    - Dream: DiffeRential Evolution Adaptive Markov chain sampler
"""

__author__="fccoelho"
__date__ ="$09/12/2009 10:44:11$"
__docformat__ = "restructuredtext en"

import numpy as np
from multiprocessing import Pool,  Process
from multiprocessing.managers import BaseManager
import scipy.stats as st
import sys
from random import sample
import xmlrpclib
from BIP.Viz.realtime import rpc_plot


class _Sampler(object):
    '''
    Base classe for all samplers
    Holds common logic and 
    '''
    _po = None
    _dimensions = None #cache for dimensions
    trace_acceptance = False
    trace_convergence = False
    seqhist = None
    liklist = []
    _j=-1
    _R = np.inf #Gelman Rubin Convergence
    def __init__(self,  parpriors=[],  parnames = []):
        self.parpriors = parpriors
        self.parnames = parnames

    @property
    def DIC(self):
        """
        Calculates  the deviance information criterion
        """
        D = -2*np.array(self.liklist)
        Dbar = np.nan_to_num(D).mean()
        meanprop = np.array([self.meld.post_phi[i].mean(axis=0) for i in self.meld.post_phi.dtype.names])
        pd = Dbar+2*self.meld._output_loglike(meanprop.T, self.data, self.likfun, self.likvariance)
        DIC = pd +Dbar
        return DIC
    
    @property
    def dimensions(self):
        if not self._dimensions:
            self._dimensions = len(self.parpriors)
        return self._dimensions
    
    @property
    def po(self):
        '''
        Pool of processes for parallel execution of tasks
        Remember to call self.term_pool() when done.
        '''
        if self._po == None:
            self._po = Pool()
        else:
            if self._po._state:
                self._po = Pool() #Pool has been terminated
        return self._po

    def term_pool(self):
        if self._po == None:
            return
        if not self._po._state: #Pool needs terminating
            self._po.close()
            self._po.join()
            self._po = None
    
    def gr_R(self, end, start):
        if self._j == end:
            return self._R
        else:
            self.gr_convergence(end, start)
            self._j = end
            return self._R
        
    def gr_convergence(self, relevantHistoryEnd, relevantHistoryStart):
        """
        Gelman-Rubin Convergence
        """
        start = relevantHistoryStart
        end = relevantHistoryEnd
        N = end - start
        if N==0:
            self._R = np.inf*np.ones(self.nchains)
            return
        N = min(min([len(self.seqhist[c]) for c in range(self.nchains)]), N)
        seq = [self.seqhist[c][-N:] for c in range(self.nchains)]
        sequences = np.array(seq) #this becomes an array (nchains,samples,dimensions)
        variances  = np.var(sequences,axis = 1)#array(nchains,dim)
        means = np.mean(sequences, axis = 1)#array(nchains,dim)
        withinChainVariances = np.mean(variances, axis = 0)
        betweenChainVariances = np.var(means, axis = 0) * N
        varEstimate = (1 - 1.0/N) * withinChainVariances + (1.0/N) * betweenChainVariances
        self._R = np.sqrt(varEstimate/ withinChainVariances)
    
    @np.vectorize
    def _accept(self, last_lik,  lik):
        """
        Decides whether to accept a proposal
        """
        if last_lik == None: last_lik = -np.inf
        # liks are logliks
        if lik == -np.inf:#0:
            return 0
        if last_lik >-np.inf:#0:
            alpha = min( np.exp(lik-last_lik), 1)
            #alpha = min(lik-last_lik, 1)
        elif last_lik == -np.inf:#0:
            alpha = 1
        else:
            return 0
            raise ValueError("Negative likelihood!?!")
#        print "last_lik, lik, alpha: ",  last_lik, lik, alpha
        if np.random.random() < alpha:
            return 1
        else:
            return 0

    def setup_xmlrpc_plotserver(self):
        """
        Sets up the server for real-time chain watch
        """
        p=0
        while p==0:
            p = rpc_plot()
        self.pserver = xmlrpclib.ServerProxy('http://localhost:%s'%p)

    def _watch_chain(self, j):
        if j<100:
            return
        self.gr_convergence(j, j-100)
        self.pserver.clearFig()
        thin = j//500 if j//500 !=0 else 1 #history is thinned to show at most 500 points, equally spaced over the entire range
        data = self.history[:j:thin].T.tolist()
        self.pserver.plotlines(data,range(j-(len(data[0])), j), self.parnames, "Chain Progress. GR Convergence: %s"%self._R,'points' , 1)

    def _tune_likvar(self, ar):
        try:
            self.arhist.append(ar)
        except AttributeError:
            self.tsig = 1
            self.tstep = .05
            self.arhist = [ar]
        dev = (0.35-ar)**2
        if dev > 0.02:
            self.likvariance *= 1+self.tsig *(.5*(np.tanh(8*dev-3)+1))
        else: return #ar at target, don't change anything
        improv = (0.35-np.mean(self.arhist[-5:-1]))**2 - (0.35-ar)**2
        if improv < 0:
            self.tsig *= -1 #change signal if AR is not improving
            self.tstep = .05 #reset to small steps if changing direction
        elif improv  > 0 and improv <.01:
            if np.random.random() <.05: #1 in 20 chance to change direction if no improvements
                self.tsig *= -1 #change signal if AR is not improving
        elif improv > 0.01:
            self.tstep *= 0.97 #reduce step if approacching sweet spot

    def _propose(self, step, po=None):
        """
        Generates proposals.
        returns two lists
        
        :Parameters:
            - `step`: Position in the markov chain history.
            - `po`: Process pool for parallel proposal generation
            
        :Returns:
            - `theta`: List of proposed self.dimensional points in parameter space
            - `prop`: List of self.nchains proposed phis.
        """
        thetalist = []
        proplist = []
        initcov = np.identity(self.dimensions)
        for c in range(self.nchains):
            if step <= 1 or self.seqhist[c] ==[]: 
                #sample from the priors
                while 1:
                    theta = [self.parpriors[dist]() for dist in self.parnames]
                    if sum ([int(np.greater(t, self.parlimits[i][0]) and np.less(t, self.parlimits[i][1])) for i, t in enumerate(theta)]) == self.dimensions:
                        break
                self.lastcv = initcov #assume no covariance at the beginning
            else:
                #use gaussian proposal
                if step%10==0 and len(self.seqhist[c]) >=10: #recalculate covariance matrix only every ten steps
                    cv = self.scaling_factor*st.cov(np.array(self.seqhist[c][-10:]))+self.scaling_factor*self.e*np.identity(self.dimensions)
                    self.lastcv = cv
                else:
                    cv = self.lastcv
                while 1:
                    theta = np.random.multivariate_normal(self.seqhist[c][-1],cv, size=1).tolist()[0]
                    if sum ([int(np.greater(t, self.parlimits[i][0]) and np.less(t, self.parlimits[i][1])) for i, t in enumerate(theta)]) == self.dimensions:
                        break
            thetalist.append(theta)
        if po:
            proplist = [po.apply(model_as_ra, (t, self.meld.model, self.meld.phi.dtype.names)) for t in thetalist]
        else:
            proplist = [model_as_ra(t, self.meld.model, self.meld.phi.dtype.names) for t in thetalist]
        propl = [p[:self.t] for p in proplist]
        return thetalist,propl

#TODO: remove dependency on the meld object
class Metropolis(_Sampler):
    """
    Standard random-walk Metropolis Hastings sampler class
    """
    def __init__(self, meldobj, samples, sampmax,  data, t, parpriors, parnames, parlimits, likfun, likvariance, burnin, **kwargs):
        """
        MCMC based fitting

        :Parameters:
            - `samples`: Number of samples to obtain
            - `sampmax`: Maximum number of samples drawn.
            - `data`: observed time series on the model's output
            - `t`: length of the observed time series
            - `parpriors`: Dictionary with frozen distributions objects as values and parnames as keys
            - `parnames`: List of parameter names
            - `parlimits`: list of tuples with (min,max) for every parameter.
            - `likfun`: Likelihood function
            - `likvariance`: variance of the Normal likelihood function
            - `burnin`: Number of burnin samples
        """
        self.salt_band = 0.05
        self.samples = samples
        self.sampmax = sampmax
        self.parpriors = parpriors
        self.parnames = parnames
        self.parlimits = parlimits
        self.likfun = likfun
        self.likvariance = likvariance
        self.data = data
        self.meld = meldobj
        self.t = t
        self.burnin = burnin
        self.nchains = 1 
        self.phi = np.recarray((self.samples+self.burnin,t),formats=['f8']*self.meld.nphi, names = self.meld.phi.dtype.names)
        self.scaling_factor = (2.38**2)/self.dimensions
        self.e = 1e-20
        if kwargs:
            for k, v in kwargs.iteritems():
                exec('self.%s = %s'%(k, v))
        self.nchains = 1 
        # Combined history of accepted samples
        self.history = np.zeros((self.nchains*(samples+self.burnin), self.dimensions)) 
        #complete history of all chains
        self.seqhist = dict([(i, [])for i in range(self.nchains)])
        #self.seqhist = np.zeros((self.nchains, self.dimensions, samples+self.burnin))
        self.setup_xmlrpc_plotserver()

    def _propose(self, step, po=None):
        """
        Generates proposals.
        returns two lists
        
        :Parameters:
            - `step`: Position in the markov chain history.
            - `po`: Process pool for parallel proposal generation
            
        :Returns:
            - `theta`: List of proposed self.dimensional points in parameter space
            - `prop`: List of self.nchains proposed phis.
        """
        thetalist = []
        proplist = []
        initcov = np.identity(self.dimensions)
        for c in range(self.nchains):
            if step <= 1 or self.seqhist[c] ==[]: 
                #sample from the priors
                while 1:
                    theta = [self.parpriors[par].rvs() for par in self.parnames]
                    if sum ([int(np.greater(t, self.parlimits[i][0]) and np.less(t, self.parlimits[i][1])) for i, t in enumerate(theta)]) == self.dimensions:
                        break
                self.lastcv = initcov #assume no covariance at the beginning
            else:
                #use gaussian proposal
                if step%10==0 and len(self.seqhist[c]) >=10: #recalculate covariance matrix only every ten steps
                    cv = self.scaling_factor*st.cov(np.array(self.seqhist[c][-10:]))+self.scaling_factor*self.e*np.identity(self.dimensions)
                    self.lastcv = cv
                else:
                    cv = self.lastcv
                while 1:
                    theta = np.random.multivariate_normal(self.seqhist[c][-1],cv, size=1).tolist()[0]
                    if sum ([int(np.greater(t, self.parlimits[i][0]) and np.less(t, self.parlimits[i][1])) for i, t in enumerate(theta)]) == self.dimensions:
                        break
            thetalist.append(theta)
        if po:
            proplist = [po.apply(model_as_ra, (t, self.meld.model, self.meld.phi.dtype.names)) for t in thetalist]
        else:
            proplist = [model_as_ra(t, self.meld.model, self.meld.phi.dtype.names) for t in thetalist]
        propl = [p[:self.t] for p in proplist]
        return thetalist,propl

    def step(self,  nchains=1):
        """
        Does the actual sampling loop.
        """
        ptheta = np.recarray(self.samples+self.burnin,formats=['f8']*self.dimensions, names = self.parnames)
        i=0;j=0;rej=0;ar=0 #total samples,accepted samples, rejected proposals, acceptance rate
        last_lik = None
        while j < self.samples+self.burnin:
            #generate proposals
            theta,prop = self._propose(j, self.po)
            #calculate likelihoods
            lik = [self.meld._output_loglike(p, self.data, self.likfun, self.likvariance, self.po) for p in prop]

#            print "lik:" , lik,  last_lik,  j
            accepted = self._accept(self, last_lik, lik)# have to include self in the call because method is vectorized.
#            print "acc:", accepted,  theta
            #Decide whether to accept proposal
            if last_lik == None: #on first sample
                last_lik = lik
                continue
            i +=self.nchains
            if sum(accepted) < self.nchains:
                rej += self.nchains-sum(accepted) #adjust rejection counter
                if i%100 == 0: 
                    ar = (i-rej)/float(i)
                    self._tune_likvar(ar)
                    if self.trace_acceptance:
                        print "--> %s: Acc. ratio: %s"%(rej, ar)
            # Store accepted values
#            print "nchains:", self.nchains
            for c, t,pr,  a in zip(range(self.nchains), theta, prop, accepted): #Iterates over the results of each chain
                #if not accepted repeat last value
                if not a:
                    continue
                self.history[j, :] = t 
                self.seqhist[c].append(t)
                #self.seqhist[c, :, j] = t 
                self.phi[j] = pr[0] if self.t==1 else [tuple(point) for point in pr]
                ptheta[j] = tuple(t)
                self.liklist.append(lik[c])
                if j == self.samples+self.burnin:break
                j += 1 #update accepted sample counter 
            #print j,  len(self.seqhist[0])
            if j%100==0 and j>0:
                if self.trace_acceptance:
                    print "++>%s,%s: Acc. ratio: %s"%(j,i, ar)
                    self._watch_chain(j)
                if self.trace_convergence: print "++> %s: Likvar: %s\nML:%s"%(j, self.likvariance, np.max(self.liklist) )
#            print "%s\r"%j
            last_lik = lik
            last_prop = prop
            last_theta = theta
            ar = (i-rej)/float(i)
        self.term_pool()
        self.meld.post_theta = ptheta[self.burnin:]
        self.meld.post_phi = self.phi[self.burnin:]
        self.meld.post_theta = ptheta#self._imp_sample(self.meld.L,ptheta,liklist)
        self.meld.likmax = max(self.liklist)
        self.meld.DIC = self.DIC
        print "Total steps(i): ",i,"rej:",rej, "j:",j
        print ">>> Acceptance rate: %s"%ar
        self.term_pool()
        self.pserver.close_plot()
        return 1
    
 
    def _rms_fit(self, s1, s2):
        '''
        Calculates a basic fitness calculation between a model-
        generated time series and a observed time series.
        It uses a normalized RMS variation.

        :Parameters:
            - `s1`: model-generated time series. 
            - `s2`: observed time series. dictionary with keys matching names of s1
        :Types:
            - `s1`: Record array or list.
            - `s2`: Dictionary or list
        
        s1 and s2 can also be both lists of lists or lists of arrays of the same length.

        :Return:
            Inverse of the Root mean square deviation between `s1` and `s2`.
        '''
        if isinstance(s1, np.recarray):
            assert isinstance(s2, dict)
            err = []
            for k in s2.keys():
                e = np.sqrt(np.mean((s1[k]-s2[k])**2.))
                err.append(e) 
        if isinstance(s1, list):
            assert isinstance(s2, list) and len(s1) ==len(s2)
            err = [np.sqrt(np.mean((s-t)**2.)) for s, t in zip(s1, s2)]
        rmsd = np.mean(err)
        fit = 1./rmsd #fitness measure
#        print "rmsd, fit, err: ", rmsd,fit, err
        if fit ==np.inf:
            sys.exit()
        return fit #mean r-squared

    @np.vectorize
    def _accept(self, last_lik, lik):
        """
        Decides whether to accept a proposal
        """
        if last_lik == None: last_lik = -np.inf
        # liks are logliks
        if lik == -np.inf:#0:
            return 0
        if last_lik >-np.inf:#0:
            alpha = min( np.exp(lik-last_lik), 1)
            #alpha = min(lik-last_lik, 1)
        elif last_lik == -np.inf:#0:
            alpha = 1
        else:
            return 0
            raise ValueError("Negative likelihood!?!")
#        print "last_lik, lik, alpha: ",  last_lik, lik, alpha
        if np.random.random() < alpha:
            return 1
        else:
            return 0

    def _imp_sample(self,n,data, w):
        """
        Importance sampling

        :Parameters:
            - `n`: Number of samples to return
            - `data`: record array (containing on or more vectors of data) to be resampled
            - `w`: Weight vector
        :Returns:
            returns a sample of size n
        """
        #sanitizing weights
        print "Starting importance Sampling"
        w /= sum(w)
        w = np.nan_to_num(w)
        j=0
        k=0
        nvar = len(data.dtype.names)
        smp = np.recarray(n,formats = [data.dtype.descr[0][1]]*nvar,names = data.dtype.names)
        #smp = copy.deepcopy(data[:n])
        while j < n:
            i = np.random.randint(0,w.size)# Random position of w
            if np.random.random() <= w[i]:
                smp[j] = data[j]
                j += 1
            k+=1
        print "Done importance sampling."
        return smp

    def _watch_chain(self, j):
        if j<100:
            return
        self.gr_convergence(j, j-100)
        self.pserver.clearFig()
        thin = j//500 if j//500 !=0 else 1 #history is thinned to show at most 500 points, equally spaced over the entire range
        data = self.history[:j:thin].T.tolist()
        self.pserver.plotlines(data,range(j-(len(data[0])), j), self.parnames, "Chain Progress. GR Convergence: %s"%self._R,'points' , 1) 

    def _add_salt(self,dataset,band):
        """
        Adds a few extra uniformly distributed data 
        points beyond the dataset range.
        This is done by adding from a uniform dist.

        :Parameters:
            - `dataset`: vector of data
            - `band`: Fraction of range to extend: [0,1[
        :Returns:
            Salted dataset.
        """
        dmax = max(dataset)
        dmin = min(dataset)
        drange = dmax-dmin
        hb = drange*band/2.
        d = numpy.concatenate((dataset,stats.uniform(dmin-hb,dmax-dmin+hb).rvs(self.K*.05)))
        return d


def model_as_ra(theta, model, phinames):
    """
    Does a single run of self.model and returns the results as a record array
    """
    theta = list(theta)
    nphi = len(phinames)
    r = model(theta)
    res = np.recarray(r.shape[0],formats=['f8']*nphi, names = phinames)
    for i, n in enumerate(res.dtype.names):
        res[n] = r[:, i]
    return res
    
class Dream(_Sampler):
    '''
    DiffeRential Evolution Adaptive Markov chain sampler
    '''
    def __init__(self, meldobj, samples, sampmax, data, t , parpriors, parnames, parlimits,likfun, likvariance, burnin, thin = 5, convergenceCriteria = 1.1,  nCR = 3, DEpairs = 1, adaptationRate = .65, eps = 5e-6, mConvergence = False, mAccept = False, **kwargs):
        self.meld = meldobj
        self.samples = samples
        self.sampmax = sampmax
        self.data = data
        self.t = t
        self.parpriors = parpriors
        self.parnames = parnames
        self.parlimits = parlimits
        self.likfun = likfun
        self.likvariance = likvariance
        self.burnin = burnin
        self.nchains = len(parpriors)
        self.phi = np.recarray((self.samples+self.burnin,t),formats=['f8']*self.meld.nphi, names = self.meld.phi.dtype.names)
        self.nCR = nCR
        self.DEpairs = DEpairs
        self.delayRej = 1
        if kwargs:
            for k, v in kwargs.iteritems():
                exec('self.%s = %s'%(k, v))
        self._R = np.array([2]*self.nchains) #initializing _R
        self.maxChainDraws = np.floor(samples/self.nchains)
        #initialize the history arrays
        # History of log posterior probs for all chains
        self.omega = np.zeros((self.samples+self.burnin, self.nchains))
        # Combined history of accepted samples
        self.history = np.zeros((self.nchains*(samples+self.burnin), self.dimensions))
        self.seqhist = dict([(i, [])for i in range(self.nchains)])
        #self.sequenceHistories = np.zeros((self.nchains, self.dimensions, self.maxChainDraws))
        # initialize the temporary storage vectors
        self.currentVectors = np.zeros((self.nchains, self.dimensions))
        self.currentLiks = np.ones(self.nchains)*-np.inf
        self.scaling_factor = 2.38/np.sqrt(2*DEpairs*self.dimensions)
        self.setup_xmlrpc_plotserver()
        
    def _det_outlier_chains(self, step):
        """
        Determine which chains are outliers
        """
        means = self.omega[step//2:step,:].mean(axis=0)
        q1 = st.scoreatpercentile(self.omega[step//2:step,:], 25)
        q3 = st.scoreatpercentile(self.omega[step//2:step,:], 75)
        iqr = q3-q1
        outl = means<q1-2*iqr
        return outl
        
    def delayed_rejection(self):
        pass
        #TODO: Implement Delayed Rejection
    def update_CR_dist(self):
        t = 1
        Lm = 0 
        pm =1./self.nCR
        
        for i in range(self.nchains):
            m = np.random.multinomial(1, [pm]*self.nCR).nonzero()[0][0]+1
            CR = float(m)/self.nCR
            Lm +=1
            #TODO: finish implementing this
    
    def _prop_initial_theta(self, step):
        """
        Generate Theta proposals from priors
        """
        thetalist = []
        initcov = np.identity(self.dimensions)
        for c in range(self.nchains):
            #sample from the priors
            while 1:
                theta = [self.parpriors[par].rvs() for par in self.parnames]
                if sum ([int(np.greater(t, self.parlimits[i][0]) and np.less(t, self.parlimits[i][1])) for i, t in enumerate(theta)]) == self.dimensions:
                    break
            self.lastcv = initcov #assume no covariance at the beginning

            thetalist.append(theta)
        return thetalist 
    
    def _prop_phi(self, thetalist, po=None):
        """
        Returns proposed Phi derived from theta
        """
        if po:
            proplist = [po.apply(model_as_ra, (t, self.meld.model, self.meld.phi.dtype.names)) for t in thetalist]
        else:
            proplist = [model_as_ra(t, self.meld.model, self.meld.phi.dtype.names) for t in thetalist]
        propl = [p[:self.t] for p in proplist]
        return propl
    
    def _chain_evolution(self, proptheta,  propphi, pps, liks):
        """
        Chain evolution as describe in ter Braak's Dream algorithm.
        """
        CR = 1./self.nCR
        b = [(l[1]-l[0])/5. for l in self.parlimits]
        delta = (self.nchains-1)//2
        gam = 2.38/np.sqrt(2*delta*self.dimensions)
        zis = []
        for c in range(self.nchains):
            e = [st.uniform(-i, 2*i).rvs() for i in b]
            eps = [st.norm(0, i).rvs() for i in b]
            others = [x for i, x in enumerate(proptheta) if i !=c]
            dif = np.zeros(self.dimensions)
            for d in range(delta):
                    d1, d2 = sample(others, 2)
                    dif+=np.array(d1)-np.array(d2)
            zi = np.array(proptheta[c])+(np.ones(self.dimensions)+e)*gam*dif+eps
            for i in range(len(zi)): #Cross over
                zi[i] = proptheta[c][i] if np.random.rand() < 1-CR else zi[i]
            zis.append(zi)
        #get the associated Phi's
        propphi_z = self._prop_phi(zis)
        evolved = [] #evolved Theta
        zprobs,  zliks = self._get_post_prob(zis, propphi_z)
        prop_evo = []
        liks_evo = []
        pps_evo = np.zeros(self.nchains) #posterior probabilities
        accepted = self._accept(self, pps, zprobs)#have to pass self because method is vectorized

        #Store results
        i = 0
        for z, a, x in zip(zis, accepted, proptheta):
            if a:
                evolved.append(z)
                prop_evo.append(propphi_z[i])
                pps_evo[i] = zprobs[i]
                liks_evo.append(zliks[i])
                self.liklist.append(zliks[i])
            else:
                evolved.append(x)
                prop_evo.append(propphi[i])
                self.liklist.append(liks[i])
                liks_evo.append(liks[i])
                try:
                    pps_evo[i] = pps[i]
                except TypeError: #when pps == None
                    pps_evo[i] = -np.inf
            i += 1
        return evolved, prop_evo, pps_evo, liks_evo, accepted
        
    def _get_post_prob(self, theta, prop, po = None):
        '''
        Calculates the posterior probability for the proposal of each chain
        
        :Parameters:
            - `theta`: list of nchains thetas
            - `prop`: list of nchains phis
            - `po`: Pool of processes
            
        :Returns:
            - `posts`: list of log posterior probabilities of length self.nchains
            - `listoliks`: list of log-likelihoods of length self.nchains
        '''
        pri = 1
        pris = []
        for c in range(self.nchains):#iterate over chains
            for i in range(len(theta[c])):
                try:
                    pri *= self.parpriors[self.parnames[i]].pdf(theta[c][i])
                except AttributeError: #in case distribution is discrete
                    pri *= self.parpriors[self.parnames[i]].pmf(theta[c][i])
            pris.append(pri)
        if po:
            listoliks = [po.apply_async(self.meld._output_loglike, (p, self.data, self.likfun, self.likvariance)) for p in prop]
            listoliks = [l.get() for l in listoliks]
            self.term_pool()
        else:
            listoliks = [self.meld._output_loglike(p, self.data, self.likfun, self.likvariance) for p in prop]
#        Multiply by prior values to obtain posterior probs
#        Actually sum the logs
        posts = (np.log(np.array(pris))+np.array(listoliks)).tolist()
        return posts, listoliks
        
    def step(self):
        """
        Does the actual sampling loop.
        """
        ptheta = np.recarray(self.samples+self.burnin,formats=['f8']*self.dimensions, names = self.parnames)
        i = 0;j=0;rej=0;ar=0 #total samples,accepted samples, rejected proposals, acceptance rate
        last_pps = None
        while j < self.samples+self.burnin:
            #generate proposals
            if j == 0:
                theta = self._prop_initial_theta(j)
                prop = self._prop_phi(theta, self.po)
                pps,  liks = self._get_post_prob(theta, prop)
            else:
                theta = [self.seqhist[c][-1] for c in range(self.nchains)]
                prop = self._prop_phi(theta, self.po)
            # Evolve chains
#            while sum(self._R <=1.2)<self.nchains:
            theta, prop, pps, liks, accepted = self._chain_evolution(theta, prop, pps, liks)
            #storing log post probs
#            print self.omega.shape,  pps.shape
            self.omega[j, :] = pps
            #Compute GR R
            self.gr_R(j, -j//2)
#            if sum(self._R <=1.2)==self.nchains:
#                print "Converged on all dimensions"
#                print j, self._R
            #Update last_lik
            if last_pps == None: #on first sample
                last_pps = pps
                continue
            i +=self.nchains
            if sum(accepted) < self.nchains:
                rej += self.nchains-sum(accepted) #adjust rejection counter
                if i%100 == 0: 
                    ar = (i-rej)/float(i)
                    self._tune_likvar(ar)
                    if self.trace_acceptance:
                        print "--> %s: Acc. ratio: %s"%(rej, ar)
            # Store accepted values
#            print "nchains:", self.nchains
            for c, t,pr,  a in zip(range(self.nchains), theta, prop, accepted): #Iterates over the results of each chain
                #if not accepted repeat last value
                if not a:
                    #Add something to the seqhist so that they all have the same length
                    if self.seqhist[c] == []:
                        self.seqhist[c].append(t)
                    else:
                        self.seqhist[c].append(self.seqhist[c][-1])
                    continue
                self.history[j, :] = t 
                self.seqhist[c].append(t)
                try:
                    self.phi[j] = pr[0] if self.t==1 else [tuple(point) for point in pr]
                    ptheta[j] = tuple(t)
                except IndexError:
                    print "index error",  j,  self.phi.shape

                if j == self.samples+self.burnin:break
                j += 1 #update accepted sample counter 
            # Remove Outlier Chains
            if j>10 and j < self.burnin:
                outl = self._det_outlier_chains(j)
                imax = pps.tolist().index(pps.max())
                for n, c in enumerate(outl):
                    if c:
                        theta[n] = theta[imax]
                        prop[n] = prop[imax]
                        pps [n] = pps[imax]
                        liks[n] = liks[imax]
            if j%100==0 and j>0:
                if self.trace_acceptance:
                    print "++>%s,%s: Acc. ratio: %s"%(j,i, ar)
                    self._watch_chain(j)
                if self.trace_convergence: print "++> %s: Likvar: %s\nML:%s"%(j, self.likvariance, np.max(self.liklist) )
#            print "%s\r"%j
            last_pps = pps
            last_prop = prop
            last_theta = theta
            ar = (i-rej)/float(i)
        self.term_pool()
        self.meld.post_theta = ptheta[self.burnin:]
        self.meld.post_phi = self.phi[self.burnin:]
        self.meld.post_theta = ptheta#self._imp_sample(self.meld.L,ptheta,liklist)
        self.meld.likmax = max(self.liklist)
        self.meld.DIC = self.DIC
        print "Total steps(i): ",i,"rej:",rej, "j:",j
        print ">>> Acceptance rate: %s"%ar
        self.term_pool()
        self.pserver.close_plot()
        return 1
        
if __name__ == "__main__":
    pass
