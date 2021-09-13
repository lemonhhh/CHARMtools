"""
Compartment level analysis and plot funcitons
@author zliu
@data 20210902
"""
#global dependence
import numpy as np
import pandas as pd
import multiprocess
import cooler
import cooltools
import cooltools.expected
from . import CHARMio
import bioframe

# Decay profile
# for p(s) curve use log_bins=True , otherwise(e.g. normalize distance for Hi-C matrix ) use log_bins=False
def psDataFromMat(matrix, indices=None, log_bins=True, base=1.1):
    """
    ***FUNCTION COPY FROM HICSTAFF***
    Compute distance law as a function of the genomic coordinate aka P(s).
    Bin length increases exponentially with distance if log_bins is True. Works
    on dense and sparse matrices. Less precise than the one from the pairs.
    Parameters
    ----------
    matrix : numpy.array or scipy.sparse.coo_matrix
        Hi-C contact map of the chromosome on which the distance law is
        calculated.
    indices : None or numpy array
        List of indices on which to compute the distance law. For example
        compartments or expressed genes.
    log_bins : bool
        Whether the distance law should be computed on exponentially larger
        bins.
    Returns
    -------
    numpy array of floats :
        The start index of each bin.
    numpy array of floats :
        The distance law computed per bin on the diagonal
    """

    n = min(matrix.shape)
    included_bins = np.zeros(n, dtype=bool)
    if indices is None:
        included_bins[:] = True
    else:
        included_bins[indices] = True
    D = np.array(
        [
            np.average(matrix.diagonal(j)[included_bins[: n - j]])
            for j in range(n)
        ]
    )
    if not log_bins:
        return np.array(range(len(D))), D
    else:
        n_bins = int(np.log(n) / np.log(base) + 1)
        logbin = np.unique(
            np.logspace(0, n_bins - 1, num=n_bins, base=base, dtype=np.int)
        )
        logbin = np.insert(logbin, 0, 0)
        logbin[-1] = min(n, logbin[-1])
        if n < logbin.shape[0]:
            print("Not enough bins. Increase logarithm base.")
            return np.array(range(len(D))), D
        logD = np.array(
            [
                np.average(D[logbin[i - 1] : logbin[i]])
                for i in range(1, len(logbin))
            ]
        )
        return logbin[:-1], logD

def getOEMatrix(matrix:np.ndarray)->np.ndarray:
    """
    get decay profile normalized pearson correlation matrix
    """
    n=matrix.shape[0]
    dist_matrix = np.zeros((n, n))
    _, dist_vals = psDataFromMat(matrix, log_bins=False)
    for i in range(n):
        for j in range(n):
            dist_matrix[i, j] = dist_vals[abs(j - i)]
    
    #obs/exp = obs / exp
    matrix = matrix / dist_matrix
    
    return matrix

def getPearsonCorrMatrix(matrix:np.ndarray)->np.ndarray:
    """
    get decay profile normalized pearson correlation matrix
    """
    matrix = getOEMatrix(matrix)
    matrix = np.corrcoef(matrix)
    
    return matrix

def addVec(a,b):
    if len(a) < len(b):
        c = b.copy()
        c[:len(a)] += a
    else:
        c = a.copy()
        c[:len(b)] += b
    return c

def getPsData(mcoolPath,chromlist,resolution=10000,celltype="unknown",base=1.1)->pd.DataFrame:
    matlist = [CHARMio.getMatrixFromMCOOLs(mcoolPath,genome_coord1=chrom,resolution=resolution) for chrom in chromlist]
    bin = psDataFromMat(matlist[0],base=base)[0]
    value = np.array([])
    for mat in matlist:
        value = addVec(value,psDataFromMat(mat,base=base)[1])
    return pd.DataFrame({"bin":bin,"aveCount":value,"celltype":celltype})

def cooltoolsGetRegions(refgenome)->pd.DataFrame:
    """
    get regions for compute expected, refgenome like "mm10" or "hg19" are fetched from UCSC by bioframe.
    Notice: bioframe check internet connection from google.com and needed to be alter or set proxy if you are in China
    """
    chromsizes = bioframe.fetch_chromsizes(refgenome,as_bed=True)
    cens = bioframe.fetch_centromeres(refgenome)
    arms = bioframe.split(chromsizes,cens,cols_points=['chrom','mid'])
    return arms,chromsizes

def prepareRegionsForCooler(clr:cooler.Cooler,arms:pd.DataFrame,chromsizes:pd.DataFrame):
    """
    select only chromosomes present in the cooler
    """
    chromsizes = chromsizes.set_index("chrom").loc[clr.chromnames].reset_index()
    arms = arms.set_index("chrom").loc[clr.chromnames].reset_index()
    # call this to automaticly assign names to chromosomal arms:
    arms = bioframe.parse_regions(arms)
    return arms

def cooltoolsExpected(clr:cooler.Cooler,nthreads:int,refgenome:str)->pd.DataFrame:
    """
    CHARMtools wrappers for cooltools
    resolution is the resolution of cooler
    refgenome like "mm10" or "hg19" are fetched from UCSC by bioframe.
    """
    nthreads = nthreads

    # get arms 
    chromsizes,arms = cooltoolsGetRegions(refgenome)
    arms = prepareRegionsForCooler(clr,arms,chromsizes)

    # Calculate expected interactions for chromosome arms
    with multiprocess.Pool(nthreads) as pool:
        expected = cooltools.expected.diagsum(
            clr,
            regions=arms,
            transforms={
                'balanced': lambda p: p['count'] * p['weight1'] * p['weight2']
            },
        map=pool.map
        )

    # Calculate average number of interactions per diagonal, this will be changed in the future versions of cooltools
    expected['balanced.avg'] = expected['balanced.sum'] / expected['n_valid']

    return expected,arms

def cooltoolsGetPsData(clr:cooler.Cooler,nthreads:int,refgenome:str)->pd.DataFrame:
	expected,arms = cooltoolsExpected(clr,20,"mm10")
	expected = expected[expected['region'].str.contains("chr[0-9]")]
	aggExpected = expected.groupby('diag').agg({'n_valid':'sum','count.sum':'sum','balanced.sum':'sum'}).reset_index()
	# Convert indices of diagonals into genomic separation, expressed in basepairs.
	aggExpected['s_bp'] = (
	    aggExpected['diag']
	    * clr.binsize)

	# Now we can calculate the average raw interaction counts and normalized contact frequencies.
	aggExpected['count.avg'] = (
	    aggExpected['count.sum']
	    / aggExpected['n_valid']
	)

	aggExpected['balanced.avg'] = (
	    aggExpected['balanced.sum']
	    / aggExpected['n_valid']
	)
	# Logbin-expected aggregates P(s) curves per region over exponentially increasing distance bins.
	lb_cvd, lb_slopes, lb_distbins = cooltools.expected.logbin_expected(expected)

	# The resulting table contains P(s) curves for each individual region.
	# Aggregating these curves into a single genome-wide curve is involving too,
	# so we created a separate function for this too.
	lb_cvd_agg, lb_slopes_agg = cooltools.expected.combine_binned_expected(
	    lb_cvd,
	    binned_exp_slope=lb_slopes
	)

	lb_cvd_agg['s_bp'] = lb_cvd_agg['diag.avg'] * clr.binsize
	lb_slopes_agg['s_bp'] = lb_slopes_agg['diag.avg'] * clr.binsize
	return aggExpected,lb_slopes_agg