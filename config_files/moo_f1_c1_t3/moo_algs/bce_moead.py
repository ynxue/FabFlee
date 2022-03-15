# =========================================================================================================

#  Bi-criterion evolution based MOEA/D

#  Please find details of BCE in the following paper
#  M. Li, S. Yang, and X. Liu. Pareto or non-Pareto: Bi-criterion evolution in multi-objective optimization.
#  IEEE Transactions on Evolutionary Computation, vol. 20, no. 5, pp. 645-665, 2016.


#  Please find details of this MOEA/D version in the following paper
#  H. Li and Q. Zhang. Multiobjective optimization problems with complicated Pareto sets, MOEA/D and NSGA-II.
#  IEEE Transactions on Evolutionary Computation, vol. 13, no. 2, pp. 284-302, 2009.

# =========================================================================================================

import numpy as np
from scipy.spatial.distance import cdist

from pymoo.algorithms.base.genetic import GeneticAlgorithm
from pymoo.core.duplicate import DefaultDuplicateElimination
from pymoo.core.population import Population
from pymoo.core.selection import Selection
from pymoo.docs import parse_doc_string
from pymoo.operators.crossover.sbx import SimulatedBinaryCrossover
from pymoo.operators.mutation.pm import PolynomialMutation
from pymoo.operators.sampling.rnd import FloatRandomSampling
from pymoo.util.display import MultiObjectiveDisplay

import math
from pymoo.util.nds.non_dominated_sorting import NonDominatedSorting
from pymoo.util.dominator import Dominator
from pymoo.core.individual import Individual
# modified Tchebycheff approach
from moo_algs.tchebicheff import Tchebicheff2


def normalize_pop(pop):
    pop_obj = pop.get("F")
    pc_size, nobj = pop_obj.shape

    fmax = np.max(pop_obj, axis=0)   # max of each column
    fmin = np.min(pop_obj, axis=0)

    for i in range(nobj):
        if fmax[i] == fmin[i]:
            for row in pop_obj:
                row[i] = 0.0

        else:
            for row in pop_obj:
                row[i] = (row[i] - fmin[i]) / (fmax[i] - fmin[i])

    pop.set("F", pop_obj)

    return pop


def normalize_bothpop(pc_pop, npc_pop):
    PCObj = pc_pop.get("F")
    NPCObj = npc_pop.get("F")

    pc_size = PCObj.shape[0]
    npc_size = NPCObj.shape[0]
    nobj = PCObj.shape[1]

    fmax = np.max(PCObj, axis=0)   # max of each column
    fmin = np.min(PCObj, axis=0)

    for i in range(nobj):
        if fmax[i] == fmin[i]:
            for row in PCObj:
                row[i] = 0.0
            for row in NPCObj:
                row[i] = row[i] - fmin[i]

        else:
            for row in PCObj:
                row[i] = (row[i] - fmin[i]) / (fmax[i] - fmin[i])

            for row in NPCObj:
                row[i] = (row[i] - fmin[i]) / (fmax[i] - fmin[i])

    pc_pop.set("F", PCObj)
    npc_pop.set("F", NPCObj)

    return pc_pop, npc_pop


def determine_radius(d, pc_size, pc_capacity):
    k_closest = 3

    # record the distance of the individual to its kth closest individual
    closest_dist = np.zeros((pc_size, k_closest))
    closest_dist = np.full((pc_size, k_closest), np.inf)

    # record the average distance of an individual to its kth closest individual in the population
    ave_dist = np.zeros(k_closest)
    d = np.sort(d)
    size = min(k_closest, pc_size)
    closest_dist = d[:, :size]
    ave_dist = np.mean(closest_dist, axis=0)

    j = size - 1
    radius = ave_dist[j]
    while ave_dist[j] == np.inf:
        j -= 1
        radius = ave_dist[j]

    return radius


def update_PCpop(pc_pop, off):

    pc_objs = pc_pop.get("F")
    off_objs = off.get("F")
    n = pc_objs.shape[0]

    del_ind = []

    for i in range(n):
        flag = Dominator.get_relation(off_objs[0, :], pc_objs[i, :])
        if flag == 1:
            # off dominates pc_pop[i]
            del_ind.append(i)
            break

        elif flag == -1:
            # pc_pop[i] dominates off
            return pc_pop
        else:
            # flag == 0
            # off and pc_pop[i] are nondominated
            break

    if len(del_ind) > 0:
        pc_index = np.arange(n)
        # Delete element at index positions given by the list 'del_ind'
        pc_index = np.delete(pc_index, del_ind)
        pc_pop = pc_pop[pc_index.tolist()]

    pc_pop = Population.merge(pc_pop, off)

    return pc_pop


# update the NPC population by the individual from the PC evolution
def update_NPCpop(npc_pop, off, ideal_point, ref_dirs, decomp):

    # calculate the decomposed values for each individual in NPC population

    FV = decomp.do(F=npc_pop.get("F"), weights=ref_dirs,
                   ideal_point=ideal_point)

    off_FV = decomp.do(F=off.get("F"), weights=ref_dirs,
                       ideal_point=ideal_point)

    # get the absolute index in F where offspring is better than the current F (decomposed space)
    I = np.where(off_FV < FV)[0]

    # update at most one solution in NPC
    if len(I) > 0:
        i = np.random.permutation(I)[0]
        npc_pop[i] = off[0]

    return npc_pop

def maintain_PCpop(PCPop, pc_capacity):

    pc_pop = PCPop.copy(deep=True)
    # Normalise the PC poulation
    pc_pop = normalize_pop(pc_pop)

    ######################################################
    # Calculate the Euclidean distance among individuals
    ######################################################

    pc_size = len(pc_pop)
    PCObj = pc_pop.get("F")

    distance = np.zeros((pc_size, pc_size))
    distance = cdist(PCObj, PCObj, 'euclidean')
    distance[distance == 0] = np.inf

    # calculate the radius for population maintenance
    radius = determine_radius(distance, pc_size, pc_capacity)

    # initialisation of PC individuals' crowding degree
    crowd_degree = np.ones(pc_size)
    current_size = pc_size

    while current_size > pc_capacity:
        max_crowding = -1
        del_ind = np.array([])
        pc_index = np.arange(current_size)

        d = np.where(distance < radius, distance / radius, 1)
        crowd_degree = 1 - np.prod(d, axis=1)

        # find the individual with the highest crowding degree in the current PC population
        max_crowding = np.amax(crowd_degree)

        if max_crowding == 0:
            # this means that all the remaining individuals are not neighboring to each other
            # in this case, randomly remove some until the PC size reduces to the capacity

            if current_size > pc_capacity:
                # record individual that should be removed from the PC population
                num = current_size - pc_capacity
                del_ind = np.random.permutation(pc_index)[:num]

            # delete element at index positions given by the list 'del_ind'
            pc_index = np.delete(pc_index, del_ind)

            pc_pop = pc_pop[pc_index.tolist()]
            PCPop = PCPop[pc_index.tolist()]

            current_size = pc_capacity

        else:
            index = np.where(crowd_degree == np.amax(
                crowd_degree))  # (array([3]),)

            # record individual that should be removed from the PC population
            del_ind = index[0][0]

            distance = np.delete(distance, del_ind, axis=0)
            distance = np.delete(distance, del_ind, axis=1)

            # delete element at index positions given by the list 'del_ind'
            pc_index = np.delete(pc_index, del_ind)

            pc_pop = pc_pop[pc_index.tolist()]
            PCPop = PCPop[pc_index.tolist()]

            current_size -= 1

    return PCPop


# =========================================================================================================
# Neighborhood Selection
# =========================================================================================================

class NeighborhoodSelection(Selection):

    def __init__(self, neighbors, prob=1.0) -> None:
        super().__init__()
        self.neighbors = neighbors
        self.prob = prob

    def _do(self, pop, n_select, n_parents, k=None, **kwargs):
        if k is None:
            k = np.random.permutation(len(pop))[:n_select]
        assert len(k) == n_select

        N = self.neighbors
        P = np.full((n_select, n_parents), -1)

        for i, j in enumerate(k):

            if np.random.random() < self.prob:
                P[i] = np.random.choice(N[j], n_parents, replace=False)
            else:
                P[i] = np.random.permutation(len(pop))[:n_parents]

        return P


# =========================================================================================================
# Implementation
# =========================================================================================================

class BCEMOEAD(GeneticAlgorithm):

    def __init__(self,
                 ref_dirs,
                 n_neighbors=20,
                 decomposition=Tchebicheff2(),
                 prob_neighbor_mating=0.9,
                 sampling=FloatRandomSampling(),
                 crossover=SimulatedBinaryCrossover(prob=1.0, eta=20),
                 mutation=PolynomialMutation(prob=None, eta=20),
                 display=MultiObjectiveDisplay(),
                 **kwargs):
        """
        Parameters
        ----------
        ref_dirs
        n_neighbors
        decomposition
        prob_neighbor_mating
        display
        kwargs
        """

        self.ref_dirs = ref_dirs
        self.pc_capacity = len(ref_dirs)
        self.pc_pop = Population.new()
        self.npc_pop = Population.new()
        self.n_neighbors = min(len(ref_dirs), n_neighbors)
        self.prob_neighbor_mating = prob_neighbor_mating
        self.decomp = decomposition

        # initialise the neighborhood of subproblems based on the distances of weight vectors
        self.neighbors = np.argsort(cdist(
            self.ref_dirs, self.ref_dirs), axis=1, kind='quicksort')[:, :self.n_neighbors]

        self.selection = NeighborhoodSelection(
            self.neighbors, prob=prob_neighbor_mating)

        super().__init__(pop_size=len(ref_dirs), sampling=sampling, crossover=crossover, mutation=mutation,
                         eliminate_duplicates=DefaultDuplicateElimination(), display=display,
                         advance_after_initialization=False, **kwargs)

    def _setup(self, problem, **kwargs):
        assert not problem.has_constraints(
        ), "This implementation of BCE_MOEAD does not support any constraints."

    def _initialize_advance(self, infills=None, **kwargs):
        super()._initialize_advance(infills, **kwargs)
        self.ideal = np.min(self.pop.get("F"), axis=0)

        # retrieve the current population
        self.npc_pop = self.pop.copy(deep=True)

        # get the objective space values and objects
        npc_objs = self.npc_pop.get("F")

        fronts, rank = NonDominatedSorting().do(npc_objs, return_rank=True)
        front_0_index = fronts[0]

        # put the nondominated individuals of the NPC population into the PC population
        self.pc_pop = self.npc_pop[front_0_index].copy(deep=True)

    def _infill(self):
        # MOEA\D inherits from genetic algorithm but does not implement the infill/advance interface
        pass

    def _advance(self, **kwargs):
        repair, crossover, mutation = self.repair, self.mating.crossover, self.mating.mutation

        pc_pop = self.pc_pop.copy(deep=True)
        npc_pop = self.npc_pop.copy(deep=True)

        ##############################################################
        # PC evolving
        ##############################################################

        # Normalise both poulations according to the PC individuals
        pc_pop, npc_pop = normalize_bothpop(pc_pop, npc_pop)

        PCObj = pc_pop.get("F")
        NPCObj = npc_pop.get("F")
        pc_size = PCObj.shape[0]
        npc_size = NPCObj.shape[0]

        ######################################################
        # Calculate the Euclidean distance among individuals
        ######################################################
        d = np.zeros((pc_size, pc_size))
        d = cdist(PCObj, PCObj, 'euclidean')
        d[d == 0] = np.inf

        # Determine the size of the niche
        if pc_size == 1:
            radius = 0
        else:
            radius = determine_radius(d, pc_size, self.pc_capacity)

        # calculate the radius for individual exploration
        r = pc_size / self.pc_capacity * radius

        ########################################################
        # find the promising individuals in PC for exploration
        ########################################################

        # promising_num: record how many promising individuals in PC
        promising_num = 0
        # count: record how many NPC individuals are located in each PC individual's niche
        count = np.array([])

        d2 = np.zeros((pc_size, npc_size))
        d2 = cdist(PCObj, NPCObj, 'euclidean')

        # Count of True elements in each row (each individual in PC) of 2D Numpy Array
        count = np.count_nonzero(d2 <= r, axis=1)

        # Check if the niche has no NPC individual or has only one NPC individual
        # Record the indices of promising individuals.
        # Since np.nonzero() returns a tuple of arrays, we change the type of promising_index to a numpy.ndarray.
        promising_index = np.nonzero(count <= 1)
        promising_index = np.asarray(promising_index).flatten()

        # Record total number of promising individuals in PC for exploration
        promising_num = len(promising_index)

        ########################################
        # explore these promising individuals
        ########################################

        original_size = pc_size
        off = Individual()

        if promising_num > 0:
            for i in range(promising_num):
                if original_size > 1:
                    parents = Population.new(2)

                    # The explored individual is considered as one parent
                    parents[0] = pc_pop[promising_index[i]]

                    # The remaining parent will be selected randomly from the PC population
                    rnd = np.random.permutation(pc_size)

                    for j in rnd:
                        if j != promising_index[i]:
                            parents[1] = pc_pop[j]
                            break

                    index = np.array([0, 1])
                    parents_shape = index[None, :]

                    # do recombination and create an offspring
                    off = crossover.do(self.problem, parents, parents_shape)[0]

                else:
                    off = pc_pop[0]

                # mutation
                off = Population.create(off)
                off = mutation.do(self.problem, off)

                # evaluate the offspring
                self.evaluator.eval(self.problem, off)

                # update the PC population by the offspring
                self.pc_pop = update_PCpop(self.pc_pop, off)

                # update the ideal point
                self.ideal = np.min(
                    np.vstack([self.ideal, off.get("F")]), axis=0)

                # update at most one solution in NPC population
                self.npc_pop = update_NPCpop(self.npc_pop, off, self.ideal,
                                             self.ref_dirs, self.decomp)

        ########################################################
        # NPC evolution based on MOEA/D
        ########################################################

        # iterate for each member of the population in random order
        for i in np.random.permutation(len(self.npc_pop)):
            # get the parents using the neighborhood selection
            P = self.selection.do(
                self.npc_pop, 1, self.mating.crossover.n_parents, k=[i])

            # perform a mating using the default operators (recombination & mutation) - if more than one offspring just pick the first
            off = self.mating.do(self.problem, self.npc_pop, 1, parents=P)[0]

            off = Population.create(off)

            # evaluate the offspring
            self.evaluator.eval(self.problem, off, algorithm=self)

            # update the PC population by the offspring
            self.pc_pop = update_PCpop(self.pc_pop, off)

            # update the ideal point
            self.ideal = np.min(np.vstack([self.ideal, off.get("F")]), axis=0)

            # now actually do the replacement of the individual is better
            self.npc_pop = self._replace(i, off)

        ########################################################
        # population maintenance operation in the PC evolution
        ########################################################

        current_pop = Population.merge(self.pc_pop, self.npc_pop)
        current_pop = Population.merge(current_pop, self.pop)

        # filter duplicate in the population
        pc_pop = self.eliminate_duplicates.do(current_pop)

        pc_size = len(pc_pop)

        if (pc_size > self.pc_capacity):

            # get the objective space values and objects
            pc_objs = pc_pop.get("F")

            fronts, rank = NonDominatedSorting().do(pc_objs, return_rank=True)
            front_0_index = fronts[0]

            # put the nondominated individuals of the NPC population into the PC population
            self.pc_pop = pc_pop[front_0_index]

            if len(self.pc_pop) > self.pc_capacity:
                self.pc_pop = maintain_PCpop(self.pc_pop, self.pc_capacity)

        self.pop = self.pc_pop.copy(deep=True)

    def _replace(self, i, off):

        npc_pop = self.npc_pop

        pop_size = len(self.ref_dirs)
        nr = math.ceil(pop_size / 100)

        # calculate the decomposed values for each neighbor
        N = self.neighbors[i]

        FV = self.decomp.do(npc_pop[N].get(
            "F"), weights=self.ref_dirs[N, :], ideal_point=self.ideal)
        off_FV = self.decomp.do(
            off.get("F"), weights=self.ref_dirs[N, :], ideal_point=self.ideal)

        # get the absolute index in F where offspring is better than the current F (decomposed space)
        I = np.where(off_FV < FV)[0]

        if len(I) > 0:
            npc_pop[N[I[:nr]]] = off[0]

        return npc_pop


parse_doc_string(BCEMOEAD.__init__)
