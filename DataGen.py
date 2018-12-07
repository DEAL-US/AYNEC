from itertools import groupby, combinations
from operator import itemgetter
from math import floor
import networkx as nx
from random import random, sample, shuffle
import os
from tqdm import tqdm, trange
from time import sleep
import matplotlib
from bokeh.io import output_file
from bokeh.plotting import figure, save
from bokeh.layouts import widgetbox
from bokeh.models.widgets import DataTable, DateFormatter, TableColumn
from bokeh.models import ColumnDataSource, HoverTool
from bokeh.transform import dodge
from bokeh.embed import json_item, file_html, components
import sys
import getopt
import json
import numpy as np
import scipy.sparse as sparse
import datetime

"""
This script generates evaluation datasets for knowledge graph completion techniques.

The following arguments can be used for simple configuration
INPUT_FILE -- The input file to read the original knowledge graph from
OUTPUT_FOLDER -- The folder where the output will be stored. If the folder does not exist, it will be created
GRAPH_FRACTION -- The overall fraction to take from the graph. The fraction is not the exact fraction, but the probability of keeping each edge.
GENERATE_NEGATIVES_TRAINING -- Whether or not negatives should be generated for the training set. If False, they are only generated for the testing set
REMOVE_INVERSES -- Whether or not detected inverses should be removed during preprocessing
MIN_NUM_REL -- Minimum frequency required to keep a relation during preprocessing
REACH_FRACTION -- Fraction of the total number of edges to keep during preprocessing, accumulating the relations, sorted by frequency. Use 1.0 to keep all edges
TESTING_FRACTION  -- Fraction used for testing
NUMBER_NEGATIVES -- Number of negatives to generate per positive
NEGATIVES_STRATEGY -- Strategy used to generate negatives. Possible: change_target, change_source, change_both_random, change_target_random, change_source_random, change_both_random, PPR
EXPORT_GEXF -- Whether or not the dataset should be exported as a gexf file, useful for visualisation
CREATE_SUMMARY -- Whether or not to create an html summary of the relations' frequency and the entities' degree
COMPUTE_PPR -- Whether or not to compute the personalised page rank (PPR) of each node in the graph. So far this is only useful when generating negatives with the "PPR" strategy, so it should be set to False if it is not used
"""


INPUT_FILE = "./datasets/FB15K/merged.txt"

OUTPUT_FOLDER = "./FB15K-reduced-20"

GRAPH_FRACTION = 0.2

GENERATE_NEGATIVES_TRAINING = True

REMOVE_INVERSES = True

MIN_NUM_REL = 2

REACH_FRACTION = 0.95

TESTING_FRACTION = 0.2

NUMBER_NEGATIVES = 1

NEGATIVES_STRATEGY = "change_target"

EXPORT_GEXF = True

CREATE_SUMMARY = True

COMPUTE_PPR = False


VERSION = "1.1.1"
# html imports for the generated html summary
bokeh_js_import = '''<link
    href="https://cdn.pydata.org/bokeh/release/bokeh-1.0.1.min.css"
    rel="stylesheet" type="text/css">
<link
    href="https://cdn.pydata.org/bokeh/release/bokeh-widgets-1.0.1.min.css"
    rel="stylesheet" type="text/css">
<link
    href="https://cdn.pydata.org/bokeh/release/bokeh-tables-1.0.1.min.css"
    rel="stylesheet" type="text/css">
<link href="https://netdna.bootstrapcdn.com/bootstrap/3.3.7/css/bootstrap.min.css" rel="stylesheet"/>

<script src="https://cdn.pydata.org/bokeh/release/bokeh-1.0.1.min.js"></script>
<script src="https://cdn.pydata.org/bokeh/release/bokeh-widgets-1.0.1.min.js"></script>
<script src="https://cdn.pydata.org/bokeh/release/bokeh-tables-1.0.1.min.js"></script>'''

class Reader():
	"""Interface used to read knowledge graphs"""

	def read(self):
		raise NotImplementedError("This function is not implemented in the base class. Please, use other classes that extend it")

class SimpleTriplesReader(Reader):
	"""Reader of simple triples files with one line per triple. Assumes the order is <relation, source, target>"""

	def __init__(self, file_path, separator, prob):
		"""
		Arguments:

		file_path -- the path to the single file containing the knwoledge graphs
		separator -- the separatior character or string used to separate the elements of the triple
		prob -- probability of keeping each triple when reading the graph. 
		If 1.0, the entire graph is kept. If lesser than one, the final graph has reduced size.
		"""

		self.file_path = file_path
		self.separator = separator
		self.prob = prob

	def read(self):
		"""
		Reads the graph using the parameters specified in the constructor.
		Expects each line to contain a triple with the relation first, then the source, then the target.

		Returns: a tuple with:
		1: a dictionary with the entities as keys (their names) as degree information as values.
		Each value is a dictionary with the outwards degree ("out_degree key"), inwards degree ("in_degree key"), and total degree ("degree" key).
		2: a set with the name of the relations in the graph
		3: a set with the edges in the graph. Each edge is a tuple with the name of the relation, the source entity, and the target entity.
		"""

		entities = dict()
		relations = set()
		edges = set()

		with open(self.file_path, "r") as file:
			for line in file:
				if(random() < self.prob):
					line = line.strip()
					triple = line.split(self.separator)
					source = triple[0]
					relationship = triple[1]
					target = triple[2]

					# Adding entities, relations and edges
					if source not in entities:
						entities[source] = dict(degree=0, out_degree=0, in_degree=0)
					if target not in entities:
						entities[target] = dict(degree=0, out_degree=0, in_degree=0)
					entities[source]["out_degree"] += 1
					entities[target]["in_degree"] += 1
					entities[source]["degree"] += 1
					entities[target]["degree"] += 1

					relations.add(relationship)
					edges.add((relationship, source, target))
		return (entities, relations, edges)

class DatasetsGenerator():
	"""
	Class used to generate the datasets.

	Methods that should be used externally:
	read -- Preprocessing: reads the knowledge graph using a reader, and performs preprocessing.
	split -- Splitting: generates training and testing sets.
	generate_negatives -- Negatives generation: adds negative examples to the training and testing sets.
	export_files -- Generates the output files excluding the gexf file.
	export_gexf -- Generates a gexf file with the evaluation datasets.
	compute_PPR -- computes the personalised page rank for each entity in the graph.
	"""

	def __init__(self, results_directory, number_splits=1):
		"""Arguments:

		results_directory -- the output spliter.
		number splits -- in case several training/set splits must be generated. Default: 1 split, corresponding to one training/set split
		"""

		self.entities = dict()
		self.relations = set()
		self.edges = set()
		self.inverses = set()
		self.inverse_tuples = list()
		self.inverses_dict = dict()
		self.graphs = dict()
		self.number_splits = number_splits
		self.ignored_rels_positives = set()
		self.results_directory = results_directory
		self.entity_edges = dict()
		self.domains = dict()
		self.ranges = dict()
		if not os.path.exists(results_directory):
			os.makedirs(results_directory)

	def group_edges(self):
		"""
		Groups the edges in a per relation basis.
		Creates and stores a dictionary with the relations as keys and the set of edges of each relation as values.
		"""

		print("\nGrouping edges")
		self.grouped_edges = dict()
		self.domains = dict()
		self.ranges = dict()
		for edge in self.edges:
			if edge[0] not in self.grouped_edges:
				self.grouped_edges[edge[0]] = set()
			if edge[0] not in self.domains:
				self.domains[edge[0]] = set()
			if edge[0] not in self.ranges:
				self.ranges[edge[0]] = set()
			self.grouped_edges[edge[0]].add((edge[1], edge[2]))
			self.domains[edge[0]].add(edge[1])
			self.ranges[edge[0]].add(edge[2])

	def read(self, reader, min_num_rel=0, reach_fraction=1, remove_inverses=False, create_summary=True):
		"""
		Reads the knowledge graph using a reader, and performs preprocessing. This function corresponds to the preprocessing step of the workflow.

		Arguments:
		min_num_rel -- minimum frequency required to keep a relation. Default: 0 (keep all).
		reach_fraction -- fraction of the total number of edges to keep, accumulating the relations, sorted by frequency. Default: 1 (keep all).
		remove_inverses -- whether or not to remove relations detected as inverses. Default: False.
		create_summary -- whether or not to create an html summary including tables and plots with the frequency of each relation and degree of each entity. Note: inverses are always included in this summary, even if removed. Default: True.

		Generates and stores:
		entities -- a dictionary with the entities as keys (their names) as degree information as values.
		Each value is a dictionary with the outwards degree ("out_degree key"), inwards degree ("in_degree key"), and total degree ("degree" key).
		relations -- a set with the name of the relations in the graph
		edges -- a set with the edges in the graph. Each edge is a tuple with the name of the relation, the source entity, and the target entity.
		the inverses as detailed in function find_inverses
		entity_edges -- a dictionary with the entities as keys, and the set of their outgoing edges as values
		"""

		print("\nReading graph")
		self.entities, self.relations, self.edges = reader.read()
		self.group_edges()
		print("\nPruning relations")
		candidate_rels = [(rel, len(instances)) for rel, instances in self.grouped_edges.items() if len(instances) >= min_num_rel]
		candidate_rels.sort(key=lambda x: x[1], reverse=True)
		accepted_rels = list()
		amounts = list()
		accumulated_fractions = list()
		accumulated_fraction = 0.0
		y_values = list()
		with tqdm(total=len(self.edges)) as pbar:
			for rel, amount in candidate_rels:
				accepted_rels.append(rel)
				amounts.append(amount)
				accumulated_fraction += amount / len(self.edges)
				accumulated_fractions.append(accumulated_fraction)
				y_values.append(accumulated_fraction)
				pbar.update(amount)
				pbar.refresh()
				if accumulated_fraction >= reach_fraction:
					break

		print(f'Kept {len(accepted_rels)} relations out of {len(self.relations)}')
		removed_rels = [rel for rel in self.relations if rel not in accepted_rels]
		print("\nRemoving small relations")
		self.remove_rels(removed_rels)
		if(create_summary):
			self.create_summary(accepted_rels, amounts, accumulated_fractions)

		self.find_inverses()
		if(remove_inverses):
			print("\nRemoving inverses")
			self.remove_rels(self.inverses)

		print("Storing outgoing edges for each node")
		for edge in tqdm(self.edges):
			if(edge[1] not in self.entity_edges):
				self.entity_edges[edge[1]] = list()
			self.entity_edges[edge[1]].append(edge)

	def compute_PPR(self, steps, alpha=0.02):
		"""
		Computes the personalised page rank of every entity, using only outward edges for paths.

		Arguments:
		steps -- the number of steps of the random walks used to compute PPR. If None, defaults to 1/alpha.
		alpha -- the teleport probability during the random walks. Increase to focus probability around each source node. Default: 0.02

		Generates and stores:
		ranks -- a matrix of size NxN where N is the number of entities. Position i,j corresponds to the probability of reaching entity j from entity i after a random walk of the given number of steps and the given teleport probability during each step.
		"""

		print("Computing movements matrix")
		self.encode(False)
		matrix_movements = np.empty([len(self.entities), len(self.entities)])
		for entity in tqdm(self.entities):
			source_ind = self.etoint[entity]
			edges = self.entity_edges.get(entity, list())
			entity_count = dict()
			for edge in edges:
				target = edge[2]
				entity_count[target] = entity_count.get(target, 0) + 1
			for target, frequency in entity_count.items():
				target_ind = self.etoint[target]
				matrix_movements[source_ind, target_ind] = frequency / len(edges)
		print("Computing PPR for every entity")
		if steps is None:
			steps = round(1 / alpha)
		initial_distribution = np.identity(len(self.entities))
		ranks = np.identity(len(self.entities))
		for _ in tqdm(range(steps)):
			ranks = (1 - alpha) * np.matmul(ranks, matrix_movements) + alpha * initial_distribution
		self.ranks = ranks

	def create_summary(self, relations, amounts, accumulated_fractions):
		"""
		Creates the html summary of the relation frequencies and entity degrees

		Arguments:
		relations -- a list with the relations to include in the summary.
		amounts -- a list with the frequency of each relation, in the same order as "relations".
		accumulated_fractions -- a list with the accumulated fraction of each relation, in the same order as "relations".
		"""

		source_relations = ColumnDataSource(data=dict(x=relations, frequencies=amounts, accumulated_fractions=accumulated_fractions))
		source_relations_table = ColumnDataSource(data=dict(x=relations, frequencies=amounts, accumulated_fractions=accumulated_fractions))
		p = figure(x_range=relations, plot_height=350, title="Relation frequency histogram")
		p.vbar(x="x", top="frequencies", width=0.9, source=source_relations)
		p.xgrid.grid_line_color = None
		p.y_range.start = 0
		p.add_tools(HoverTool(tooltips=[("Relation", "@x"), ("Frequency", "@frequencies")]))
		p.xaxis.major_tick_line_color = None  # turn off x-axis major ticks
		p.xaxis.minor_tick_line_color = None  # turn off x-axis minor ticks
		p.xaxis.major_label_text_font_size = '0pt'  # turn off x-axis tick labels
		relations_script, relations_div = components(p)
		columns = [
			TableColumn(field="x", title="Relation name"),
			TableColumn(field="frequencies", title="Frequency"),
			TableColumn(field="accumulated_fractions", title="Accumulated fraction")
		]
		data_table = DataTable(source=source_relations_table, columns=columns, width=450, height=350)
		relations_table_script, relations_table_div = components(data_table)

		entities = sorted(self.entities.items(), key=lambda x: x[1]["degree"], reverse=True)
		source_entities = ColumnDataSource(data=dict(x=[entity[0] for entity in entities], degree=[entity[1]["degree"] for entity in entities], out_degree=[entity[1]["out_degree"] for entity in entities], in_degree=[entity[1]["in_degree"] for entity in entities]))
		source_entities_table = ColumnDataSource(data=dict(x=[entity[0] for entity in entities], degree=[entity[1]["degree"] for entity in entities], out_degree=[entity[1]["out_degree"] for entity in entities], in_degree=[entity[1]["in_degree"] for entity in entities]))
		p = figure(x_range=[entity[0] for entity in entities], plot_height=350, title="Entity degree histogram")
		p.vbar(color="#c9d9d3", x=dodge('x', -0.25, range=p.x_range), top="degree", width=0.2, source=source_entities)
		p.vbar(color="#718dbf", x=dodge('x', 0, range=p.x_range), top="out_degree", width=0.2, source=source_entities)
		p.vbar(color="#e84d60", x=dodge('x', 0.25, range=p.x_range), top="in_degree", width=0.2, source=source_entities)
		p.xgrid.grid_line_color = None
		p.y_range.start = 0
		p.add_tools(HoverTool(tooltips=[("Entity", "@x"), ("Degree", "@degree"), ("Outwards degree", "@out_degree"), ("Inwards degree", "@in_degree")]))
		p.xaxis.major_tick_line_color = None  # turn off x-axis major ticks
		p.xaxis.minor_tick_line_color = None  # turn off x-axis minor ticks
		p.xaxis.major_label_text_font_size = '0pt'  # turn off x-axis tick labels
		entities_script, entities_div = components(p)
		columns = [
			TableColumn(field="x", title="Entity name"),
			TableColumn(field="degree", title="Total degree"),
			TableColumn(field="out_degree", title="Outwards degree"),
			TableColumn(field="in_degree", title="Inwards degree")
		]
		data_table = DataTable(source=source_entities_table, columns=columns, width=450, height=350)
		entities_table_script, entities_table_div = components(data_table)

		with open(self.results_directory + "/summary.html", "w") as file:
			file.write(f'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>AYNEC graph summary</title>
				{bokeh_js_import}

				{relations_script}
				{relations_table_script}
				{entities_script}
				{entities_table_script}

			</head><body><section class="container"><h1>AYNEC summary - {OUTPUT_FOLDER.split('/')[-1]}</h1><h2>Relations</h2><div class="row"><div class="col-md-7">
				{relations_div}
			</div><div class="col-md-5">
				{relations_table_div}
			</div></div><hb/><h2>Entities</h2><div class="row"><div class="col-md-7">
				{entities_div}
			</div><div class="col-md-5">
				{entities_table_div}
			</div></div></section>
			<section style="margin-top:20px" class="container"><div class="row text-center"><div class="col-md-12">Generated with AYNEC {VERSION} at {datetime.datetime.now()}. For issues or suggerences send a mail to <a href="mailto:dayala@us.es?Subject=AYNEC%20issue" target="_top">dayala1@us.es</a></div></div></section>
			</body></html>''')

	def remove_rels(self, removed_rels):
		"""
		Removes the given relations form the stored graph.

		Arguments:
		removed_rels -- the relations to be removed.
		"""
		self.edges = set(filter(lambda e: e[0] not in removed_rels, self.edges))
		with tqdm(removed_rels) as pbar:
			for rel in pbar:
				# pbar.write(rel)
				self.grouped_edges.pop(rel, None)
				self.relations.remove(rel)

	def find_inverses(self):
		"""
		Finds the inverse relations in the graph.

		Computes and stores:
		inverse_tuples -- a list of tuples with each inverse, where each tuple contains the two relations in the inverse relationship.
		inverses -- a set with the inverses, that is, the second element of the inverse tuples.
		inverses_dict -- a dictionary with the relations as keys and the sets of their inverse relations as values.

		"""

		print("\nFinding inverse relations")
		for combination in tqdm(combinations(self.relations, 2), total=len(self.relations) * (len(self.relations) - 1) / 2):
			edges1 = self.grouped_edges[combination[0]]
			edges2 = self.grouped_edges[combination[1]]
			is_inverse = all((edge[1], edge[0]) in edges2 for edge in edges1)
			is_inverse = is_inverse and all((edge[1], edge[0]) in edges1 for edge in edges2)
			if is_inverse:
				self.inverse_tuples.append((combination[0], combination[1]))
				self.inverses.add(combination[1])
		print(f'found {len(self.inverse_tuples)} inverses')
		for r1 in self.relations:
			self.inverses_dict[r1] = set()
			for r2 in self.relations:
				if (r1, r2) or (r2, r1) in self.inverse_tuples:
					self.inverses_dict[r1].add(r2)

	def encode(self, replace_names=False):
		"""
		Associates relations and entities to contiguous integers.

		Arguments:
		replace_names -- whether or not to, in addition to storing the mapping to integers, to actually replace the name of the stored entities and relations with said integers. Default: False.

		Computes and stores:
		etoint -- the dictionary with entities as keys and integers as values
		inttoe -- the dictionary with integers as keys and entities as values
		rtoint -- the dictionary with relations as keys and integers as values
		inttor -- the dictionary with integers as keys and relations as values

		"""

		self.etoint = {ent: i for i, ent in enumerate(self.entities.keys())}
		self.inttoe = {i: ent for ent, i in self.etoint.items()}
		self.rtoint = {rel: i for i, rel in enumerate(self.relations)}
		self.inttor = {i: rel for rel, i in self.rtoint.items()}
		if(replace_names):
			self.edges = [(self.etoint[s], self.rtoint[r], self.etoint[t]) for r, s, t in self.edges]
			self.relations = [self.rtoint[r] for r in self.relations]
			for e in self.entities.keys():
				self.entities[etoint[e]] = self.entities.pop(e)

			self.group_edges()

	def split_graph(self, fraction_test=0.1, fraction_test_relations={}):
		"""
		Splits the graph into training and testing sets. Creates as many different splits as given by the "number_splits" property

		Arguments:
		fraction_test_relations -- a dictionary with the fraction of each relation to take for testing. Default: an empty dictionary, which implies the same fraction for all relations (given by "fraction_test")
		fraction_test -- the fraction to take from all relations for testing. Only used if "fraction_test_relations" is empty.

		Generates and stores:
		graphs -- a dictionary with the split identifier as keys, and the training and testing sets of each split as values in a dictionary with "train" and "test" keys. Both "train" and "test" return a dictionary with, so far, only the "positive" key corresponding to the positiva edges in a set.
		"""

		print("\nDividing graph")
		train_positive = []
		test_positive = []
		if(len(fraction_test_relations) is 0):
			fraction_test_relations = {rel: fraction_test for rel in self.relations}
		number_test_relations = {}
		for i in trange(self.number_splits):
			self.graphs[i] = dict()
			self.graphs[i]["train"] = dict()
			self.graphs[i]["test"] = dict()
			self.graphs[i]["train"]["positive"] = set()
			self.graphs[i]["test"]["positive"] = set()
			for rel in tqdm(self.relations):
				edges = [(rel, s, t) for s, t in self.grouped_edges[rel]]
				offset = floor(len(edges) / self.number_splits * i)
				fraction_test = fraction_test_relations.get(rel, 0.0)
				num_test = floor(len(edges) * fraction_test)
				ids_test = [(offset + x) % len(edges) for x in range(0, num_test)]
				ids_train = [(offset + x) % len(edges) for x in range(num_test, len(edges))]
				edges_test = [edges[id] for id in ids_test]
				edges_train = [edges[id] for id in ids_train]
				self.graphs[i]["test"]["positive"].update(edges_test)
				self.graphs[i]["train"]["positive"].update(edges_train)

	def add_networkx_edges(self, split, train_test, positive_negative, entities, graph):
		"""
		Adds edges to a networkx graph.

		Arguments:
		split -- the split to add edges from.
		train_test -- whether to add the train or test edges. Should be "train" or "test".
		positive_negative -- whether to add the positive or the negative examples. Should be "positive" or "negative".
		entities -- a set with entities, used to keep track of the entities that are being added to a single graph.
		"""

		edges = self.graphs[split][train_test][positive_negative]
		entities.update([edge[1] for edge in edges])
		entities.update([edge[2] for edge in edges])
		graph_edges = [(edge[1], edge[2], {"Label": edge[0], "positive": True if positive_negative == "positive" else False, "train": True if train_test == "train" else False}) for edge in edges]
		print(f'Adding {len(graph_edges)} edges')
		graph.add_edges_from(graph_edges)

	def export_gexf(self, split, include_train, include_test, include_positive, include_negative):
		"""
		Generates and stores the gexf file in the output folder, named "dataset.gexf".

		Arguments:
		split -- the split to use as source of the graph
		include_train -- whether or not to include the training edges
		include_test -- whether or not to include the testing edges
		include_train -- whether or not to include the positive edges
		include_test -- whether or not to include the negative edges
		"""

		print("\nExporting gexf")
		g = nx.MultiDiGraph()
		entities = set()
		if(include_train):
			if(include_positive):
				self.add_networkx_edges(split, "train", "positive", entities, g)
			if(include_negative):
				self.add_networkx_edges(split, "train", "negative", entities, g)
		if(include_test):
			if(include_positive):
				self.add_networkx_edges(split, "test", "positive", entities, g)
			if(include_negative):
				self.add_networkx_edges(split, "test", "negative", entities, g)
		g.add_nodes_from(entities)
		nx.write_gexf(g, self.results_directory + "/dataset.gexf")

	def generate_negatives_PPR(self, positive, number_negatives):
		"""
		Generates negatives from a positive using the PPR strategy, which changes the source and target while keeping the domain/range of the relation.
		The candidates are selected from the PPR of each node, selecting a random one while weighting by PPR.

		Arguments:
		positive -- the positive to generate the negatives from
		number_negatives -- how many negatives to generate

		Returns: a list of negative edge examples.
		"""
		rel = positive[0]
		source_ranks = self.ranks[self.etoint[positive[1]]]
		target_ranks = self.ranks[self.etoint[positive[2]]]
		sources = [(self.inttoe[i], rank) for i, rank in enumerate(source_ranks) if rank > 0 and self.inttoe[i] in self.domains[rel]]
		targets = [(self.inttoe[i], rank) for i, rank in enumerate(target_ranks) if rank > 0 and self.inttoe[i] in self.ranges[rel]]
		sources_probs = np.array([source[1] for source in sources])
		sources_probs /= sources_probs.sum()
		targets_probs = np.array([target[1] for target in targets])
		targets_probs /= targets_probs.sum()
		ids_sources = np.random.choice(len(sources_probs), number_negatives, p=sources_probs)
		ids_targets = np.random.choice(len(targets_probs), number_negatives, p=targets_probs)
		sources = [sources[ids_sources[i]][0] for i in range(number_negatives)]
		targets = [targets[ids_targets[i]][0] for i in range(number_negatives)]
		negatives = [(rel, sources[i], targets[i]) for i in range(number_negatives)]
		return negatives

	def generate_negatives_random(self, positive, number_negatives, keep_dom_ran=True, change_source=False, change_target=True, equal_probabilities=False):
		"""
		Generates negatives from a positive by changing the source and/or target.

		Arguments:
		positive -- the positive to generate the negatives from.
		number_negatives -- how many negatives to generate.
		keep_dom_range -- whether or not to keep the domain or range when finding candidates. Default: True.
		change_source -- whether or not to change the source when generating negative examples. Default: False.
		change_target -- whether or not to change the target when generating negative examples. Default: True.
		equal_probabilities -- whether or not to give the same probability to all candidates. If False, the probability depends on the number of occurrences of each entity in the relevant position of the relation. Default: False.

		Returns: a list of negative edge examples.
		"""

		rel = positive[0]
		if(keep_dom_ran):
			if(change_source):
				candidates_source = [edge[0] for edge in self.grouped_edges[rel]]
			if(change_target):
				candidates_target = [edge[1] for edge in self.grouped_edges[rel]]
		else:
			if(change_source):
				candidates_source = [edge[0] for edge in self.edges]
			if(change_target):
				candidates_target = [edge[1] for edge in self.edges]
		if(equal_probabilities):
			if(change_source):
				candidates_source = list(self.domains[rel])
			if(change_target):
				candidates_target = list(self.ranges[rel])
		negatives = list()
		# Process for each generated negative
		for _ in range(number_negatives):
			source = None
			target = None
			# Finding a new source, if required
			if(change_source):
				attempts = 0
				found = False
				# We only try to find a new source in 20 attempts. We assume it is not possible if surpassed (all candidates are equal to the original)
				while not found and len(candidates_source) > 1 and attempts <= 20:
					# After 10 attempts, it is difficult to find a new source. The original could be very frequent. We make all probabilities equal.
					if(attempts > 10):
						if(keep_dom_ran):
							candidates_source = list(self.domains[rel])
						else:
							candidates_source = list(self.entities.keys())
					attempts += 1
					source = sample(candidates_source, 1)[0]
					found = source != positive[1]
					if not found:
						source = None
			# If not required, the source remains the same
			else:
				source = positive[1]
			# Finding a new target, if required
			if(change_target):
				attempts = 0
				found = False
				while not found and len(candidates_target) > 1 and attempts <= 20:
					if(attempts > 10):
						if(keep_dom_ran):
							candidates_target = list(self.ranges[rel])
						else:
							candidates_target = list(self.entities.keys())
					attempts += 1
					target = sample(candidates_target, 1)[0]
					found = target != positive[2]
					if not found:
						target = None
			else:
				target = positive[2]
			if(not (change_source and source is None) and not (change_target and target is None)):
				negatives.append((rel, source, target))
		return negatives

	def generate_negatives(self, split, train_test, negatives_factor, strategy, clean_before=True, reject_rel_after_failure=False):
		"""
		Generates negatives from a given set of positive examples.

		Arguments:
		split -- the split form which to generate negatives
		train_test -- whether to generate negatives form the training or testing set
		negatives_factor -- how many negatives will be generated per positive. Decimals indicate the probability of the final negative example.
		strategy -- the negatives generation strategy.
		clean_before -- whether or not remove existing negative examples for the given set, if there are any. Default: True
		reject_rel_after_failute -- whether or not ignore a relation if an attempt to generate negative examples form a positive of the relation is unable to find any, which should mean that there is only one candidate.
		"""

		print("\nGenerating negatives")
		edges = self.graphs[split][train_test]["positive"]
		if clean_before:
			self.graphs[split][train_test]["negative"] = set()
		negatives = list()
		with tqdm(edges) as pbar:
			for positive in pbar:
				if(positive[0] not in self.ignored_rels_positives):
					factor_copy = negatives_factor
					num_negatives = 0
					while random() < factor_copy:
						factor_copy -= 1.0
						num_negatives += 1
					if(strategy == "change_source"):
						new_negatives = self.generate_negatives_random(positive, num_negatives, True, True, False)
					elif(strategy == "change_target"):
						new_negatives = self.generate_negatives_random(positive, num_negatives, True, False, True)
					elif(strategy == "change_both"):
						new_negatives = self.generate_negatives_random(positive, num_negatives, True, True, True)
					elif(strategy == "change_source_random"):
						new_negatives = self.generate_negatives_random(positive, num_negatives, False, True, False)
					elif(strategy == "change_target_random"):
						new_negatives = self.generate_negatives_random(positive, num_negatives, False, False, True)
					elif(strategy == "change_both_random"):
						new_negatives = self.generate_negatives_random(positive, num_negatives, False, True, True)
					elif(strategy == "PPR"):
						new_negatives = self.generate_negatives_PPR(positive, num_negatives)
					if(len(new_negatives)) > 0:
						negatives.extend(new_negatives)
					elif(reject_rel_after_failure):
						self.ignored_rels_positives.add(positive[0])
						pbar.write(f'Ignoring relation {positive[0]}: returned no negatives in an attempt')
						pbar.refresh()
		self.graphs[split][train_test]["negative"] = negatives

	def export_files(self, split, include_train_negatives=False):
		"""
		Creates the output files, excluding the gexf files.

		Arguments:
		split -- the split to generate the output from.
		include_train_negatives -- whether or not training negatives should be included. Default: False.

		Outputs the following files:
		train.txt -- the training triples, with a triple per line, separated by tabs and with a label in the following order: <source relation target label>. Label is 1 if positive and -1 if negative.
		test.txt -- the testing triples, following the same format as train.txt
		relations.txt -- the existing relations and their frequency, sorted by frequency.
		entities.txt -- the existing entities and their degrees, sorted by total degree.
		inverses.txt -- the detected inverse relations pairs, whether or not they were removed.
		"""

		with open(self.results_directory + "/train.txt", "w") as file:
			for edge in self.graphs[split]["train"]["positive"]:
				file.write("\t".join((edge[1], edge[0], edge[2], "1")) + "\n")
			if(include_train_negatives):
				for edge in self.graphs[split]["train"]["negative"]:
					file.write("\t".join((edge[1], edge[0], edge[2], "-1")) + "\n")
		with open(self.results_directory + "/test.txt", "w") as file:
			for edge in self.graphs[split]["test"]["positive"]:
				file.write("\t".join((edge[1], edge[0], edge[2], "1")) + "\n")
			for edge in self.graphs[split]["test"]["negative"]:
				file.write("\t".join((edge[1], edge[0], edge[2], "-1")) + "\n")
		with open(self.results_directory + "/relations.txt", "w") as file:
			for rel, edges in sorted(self.grouped_edges.items(), key=lambda x: len(x[1]), reverse=True):
				file.write(f'{rel}\t{str(len(edges))}\n')
		with open(self.results_directory + "/entities.txt", "w") as file:
			for entity, degrees in sorted(self.entities.items(), key=lambda x: x[1]["degree"], reverse=True):
				file.write(f'{entity}\t{degrees["degree"]}\t{degrees["out_degree"]}\t{degrees["in_degree"]}\n')
		with open(self.results_directory + "/inverses.txt", "w") as file:
			for r1, r2 in self.inverse_tuples:
				file.write(f'{r1}\t{r2}\n')

def main():
	reader = SimpleTriplesReader(INPUT_FILE, '\t', GRAPH_FRACTION)
	generator = DatasetsGenerator(OUTPUT_FOLDER)
	generator.read(reader, min_num_rel=MIN_NUM_REL, reach_fraction=REACH_FRACTION, remove_inverses=REMOVE_INVERSES, create_summary=CREATE_SUMMARY)
	if(COMPUTE_PPR):
		generator.compute_PPR(5)
	generator.split_graph(TESTING_FRACTION)
	generator.generate_negatives(0, "test", NUMBER_NEGATIVES, NEGATIVES_STRATEGY, True, False)
	if(GENERATE_NEGATIVES_TRAINING):
		generator.generate_negatives(0, "train", NUMBER_NEGATIVES, NEGATIVES_STRATEGY, True, False)
	generator.export_files(0, True)
	if(EXPORT_GEXF):
		generator.export_gexf(0, True, True, True, True)

if __name__ == '__main__':
	main()