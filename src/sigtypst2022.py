"""Utility functions and data handling for the shared task."""

from lingpy import *
from lingpy.evaluate.acd import _get_bcubed_score as bcubed_score
from pathlib import Path
from git import Repo
from lingpy.compare.partial import Partial
import argparse
from collections import defaultdict
import random
import networkx as nx
from networkx.algorithms.clique import find_cliques
from lingpy.align.sca import get_consensus
from lingpy.sequence.sound_classes import prosodic_string, class2tokens
from lingpy.align.multiple import Multiple
from lingrex.reconstruct import CorPaRClassifier, transform_alignment
from lingrex.util import bleu_score
from itertools import combinations
from tabulate import tabulate
import json
from tqdm import tqdm as progressbar
import math


def sigtypst2022_path(*comps):
    return Path(__file__).parent.parent.joinpath(*comps)


def download(datasets, pth):
    """
    Download all datasets as indicated with GIT.
    """
    for dataset, conditions in datasets.items():
        if pth.joinpath(dataset, "cldf", "cldf-metadata.json").exists():
            print("[i] skipping existing dataset {0}".format(dataset))
        else:
            repo = Repo.clone_from(
                    "https://github.com/"+conditions["path"]+".git",
                    pth / dataset)
            repo.git.checkout(conditions["version"])
            print("[i] downloaded {0}".format(dataset))


def get_cognates(wordlist, ref):
    """
    Retrieve cognate sets from a wordlist.
    """
    etd = wordlist.get_etymdict(ref=ref)
    cognates = {}

    if ref == "cogids":
        for cogid, idxs_ in etd.items():
            idxs, count = {}, 0
            for idx, language in zip(idxs_, wordlist.cols):
                if idx:
                    tks = wordlist[idx[0], "tokens"]
                    cogidx = wordlist[idx[0], ref].index(cogid)
                    idxs[language] = " ".join([
                        x.split("/")[1] if "/" in x else x for x in
                        tks.n[cogidx]])
                    count += 1
                else:
                    idxs[language] = ""
            if count >= 2:
                cognates[cogid] = idxs

    elif ref == "cogid":
        for cogid, idxs_ in etd.items():
            idxs, count = {}, 0
            for idx, language in zip(idxs_, wordlist.cols):
                if idx:
                    tks = wordlist[idx[0], "tokens"]
                    idxs[language] = " ".join([x.split("/")[1] if "/" in x
                        else x for x in tks])
                    count += 1
                else:
                    idxs[language] = ""
            if count >= 2:
                cognates[cogid] = idxs
    return cognates


def prepare(datasets, datapath, cldfdatapath, runs=1000):
    """
    Function computes cognates from a CLDF dataset and writes them to file.
    """
    for dataset, conditions in datasets.items():
        print("[i] analyzing {0}".format(dataset))
        columns = [
            "parameter_id",
            "concept_name",
            "language_id",
            "language_name",
            "value",
            "form",
            "segments",
            "language_glottocode",
            "language_latitude",
            "language_longitude",
            "language_"+conditions["subgroup"]
            ]

        if conditions["cognates"]:
            columns += [conditions["cognates"]]
        # preprocessing to get the subset of the data
        wl = Wordlist.from_cldf(
            cldfdatapath.joinpath(dataset, "cldf", "cldf-metadata.json"),
            columns=columns
            )
        D = {0: [h for h in wl.columns]}
        for idx, subgroup in wl.iter_rows("language_"+conditions["subgroup"]):
            if subgroup == conditions["name"]:
                D[idx] = wl[idx]
        if not conditions["cognates"]:
            part = Partial(D)
            part.get_partial_scorer(runs=runs)
            part.partial_cluster(method="lexstat", threshold=0.45, ref="cogids",
                    cluster_method="infomap")
            ref = "cogids"
        elif conditions["cognates"] in ["cognacy", "partial_cognacy"]:
            part = Wordlist(D)
            ref = "cogids"
            C = {}
            for idx in part:
                C[idx] = basictypes.ints(part[idx, conditions["cognates"]])
            part.add_entries(ref, C, lambda x: x)
        else:
            part = Wordlist(D)
            ref = "cogid"
        cognates = get_cognates(part, ref)

        if datapath.joinpath(dataset).exists():
            pass
        else:
            Path.mkdir(datapath.joinpath(dataset))
        with open(datapath.joinpath(dataset, "cognates.tsv"), "w") as f:
            f.write("COGID\t"+"\t".join(part.cols)+"\n")
            for cogid, idxs in cognates.items():
                f.write("{0}".format(cogid))
                for language in part.cols:
                    f.write("\t{0}".format(idxs[language]))
                f.write("\n")
        part.output(
                "tsv", filename=datapath.joinpath(dataset, "wordlist").as_posix(), ignore="all", prettify=False)





def load_cognate_file(path):
    """
    Helper function for simplified cognate formats.
    """
    data = csv2list(path, strip_lines=False)
    header = data[0]
    languages = header[1:]
    out = {}
    sounds = defaultdict(lambda : defaultdict(list))
    for row in data[1:]:
        out[row[0]] = {}
        for language, entry in zip(languages, row[1:]):
            out[row[0]][language] = entry.split()
            for i, sound in enumerate(entry.split()):
                sounds[sound][language] += [[row[0], i]]
    return languages, sounds, out



def write_cognate_file(languages, data, path):
    with open(path, "w") as f:
        f.write("COGID\t"+"\t".join(languages)+"\n")
        for k, v in data.items():
            f.write("{0}".format(k))
            for language in languages:
                f.write("\t"+" ".join(v.get(language, [])))
            f.write("\n")



def split_training_test_data(data, languages, ratio=0.1):
    """
    Split data into test and training data.
    """
    split_off = int(len(data) * ratio + 0.5)
    cognates = [key for key, value in sorted(
        data.items(),
        key=lambda x: sum([1 if " ".join(b) not in ["", "?"] else 0 for a, b in
            x[1].items()]),
        reverse=True)
        ]
    test_, training = (
            {c: data[c] for c in cognates[:split_off]}, 
            {c: data[c] for c in cognates[split_off:]}
            )
    
    # now, create new items for all languages to be predicted
    test = defaultdict(dict)
    solutions = defaultdict(dict)
    for i, language in enumerate(languages):
        for key, values in test_.items():
            if " ".join(test_[key][language]):
                new_key = key+"-"+str(i+1)
                for j, languageB in enumerate(languages):
                    if language != languageB:
                        test[new_key][languageB] = test_[key][languageB]
                    else:
                        test[new_key][language] = ["?"]
                        solutions[new_key][language] = test_[key][language]
    
    return training, test, solutions
    

def split_data(datasets, pth, props=None):
    props = props or [0.1, 0.2, 0.3, 0.4, 0.5]

    for prop in props:
        for dataset, conditions in datasets.items():
            languages, sounds, data = load_cognate_file(
                    pth.joinpath(dataset, "cognates.tsv"))
            #data_part, solutions = split_training(data, ratio=prop)
            training, test, solutions = split_training_test_data(
                    data, languages, ratio=prop)
            write_cognate_file(
                    languages, 
                    training,
                    pth.joinpath(
                        dataset, "training-{0:.2f}.tsv".format(prop)),
                    )
            write_cognate_file(
                    languages, 
                    test,
                    pth.joinpath(
                        dataset, "test-{0:.2f}.tsv".format(prop)),
                    )
            write_cognate_file(
                    languages,
                    solutions,
                    pth.joinpath(
                        dataset, "solutions-{0:.2f}.tsv".format(prop)),
                    )
            print("[i] wrote training and solution data for {0} / {1:.2f}".format(
                dataset, prop))



def ungap(alignment, languages, proto):
    cols = []
    pidxs = []
    for i, taxon in enumerate(languages):
        if taxon == proto:
            pidxs += [i]
    merges = []
    for i in range(len(alignment[0])):
        col = [row[i] for row in alignment]
        col_rest = [site for j, site in enumerate(col) if j not in pidxs]
        if "-" in col_rest and len(set(col_rest)) == 1:
            merges += [i]
    if merges:
        new_alms = []
        for i, row in enumerate(alignment):
            new_alm = []
            mergeit = False
            started = True
            for j, cell in enumerate(row):
                if j in merges or mergeit:
                    mergeit = False
                    if not started: #j != 0:
                        if cell == "-":
                            pass
                        else:
                            if not new_alm[-1]:
                                new_alm[-1] += cell
                            else:
                                new_alm[-1] += '.'+cell
                    else:
                        mergeit = True
                        if cell == "-":
                            new_alm += [""]
                        else:
                            new_alm += [cell]
                else:
                    started = False
                    new_alm += [cell]
            for k, cell in enumerate(new_alm):
                if not cell:
                    new_alm[k] = "-"
            new_alms += [new_alm]
        return new_alms
    return alignment


def simple_align(
        seqs, 
        languages, 
        all_languages,
        align=True,
        training=True,
        missing="Ø", 
        gap="-",
        ):
    """
    Simple alignment function that inserts entries for missing data.
    """
    return transform_alignment(
            seqs, languages, all_languages, align=align,
            training=training, missing=missing, gap=gap, startend=False,
            prosody=False, position=False, firstlast=False)


class Baseline(object):

    def __init__(
            self, datapath, minrefs=2, missing="Ø", gap="-", threshold=1,
            func=simple_align):
        """
        The baseline is the prediction method by List (2019).
        """
        self.languages, self.sounds, self.data = load_cognate_file(datapath)
        self.gap, self.missing = gap, missing

        # make a simple numerical embedding for sounds
        self.classifiers = {
            language: CorPaRClassifier(minrefs, missing=0,
                    threshold=threshold) for language in self.languages}
        self.alignments = {
                language: [] for language in self.languages} 
        self.to_predict = defaultdict(list)

        for cogid, data in self.data.items():
            alms, languages = [], []
            for language in self.languages:
                if data[language] and \
                        " ".join(data[language]) != "?":
                    alms += [data[language]]
                    languages += [language]
                elif data[language] and " ".join(data[language]) == "?":
                    self.to_predict[cogid] += [language]
            for i, language in enumerate(languages):
                self.alignments[language].append(
                        [
                            cogid,
                            [lang for lang in languages if lang != language]+[language],
                            [alm for j, alm in enumerate(alms) if i != j]+[alms[i]]
                            ]
                        )
        self.func = func


    def fit(self, func=simple_align):
        """
        Fit the data.
        """
        self.patterns = defaultdict(lambda : defaultdict(list))
        self.func = func
        self.matrices = {language: [] for language in self.languages}
        self.solutions = {language: [] for language in self.languages}
        self.patterns = {
                language: defaultdict(lambda : defaultdict(list)) for
                language in self.languages}
        sounds = set()
        for language in self.languages:
            for cogid, languages, alms in self.alignments[language]:
                alm_matrix = self.func(
                        #alms, languages, self.languages,
                        alms, languages, [l for l in self.languages if l !=
                            language]+[language],
                        training=True)
                for i, row in enumerate(alm_matrix):
                    ptn = tuple(row[:len(self.languages)]+row[len(self.languages)+1:])
                    self.patterns[language][ptn][row[len(self.languages)-1]] += [(cogid, i)]

                    for sound in ptn:
                        sounds.add(sound)
                    sounds.add(row[-1])
        self.sound2idx = dict(zip(sorted(sounds), range(2, len(sounds)+2)))
        self.sound2idx[self.gap] = 1
        self.sound2idx[self.missing] = 0
        self.idx2sound = {v: k for k, v in self.sound2idx.items()}

        for language in progressbar(self.languages, desc="fitting classifiers"):
            for pattern, sounds in self.patterns[language].items():
                for sound, vals in sounds.items():
                    target = self.sound2idx[sound]
                    row = [self.sound2idx[s] for s in pattern]
                    for cogid, idx in vals:
                        self.matrices[language] += [row]
                        self.solutions[language] += [target]
            self.classifiers[language].fit(
                    self.matrices[language],
                    self.solutions[language])
    
    def predict(self, languages, alignments, target, unknown="?"):
        
        matrix = self.func(
                alignments, languages, [l for l in self.languages if l !=
                    target],
                training=False,
                )
        new_matrix = [[0 for char in row] for row in matrix]
        for i, row in enumerate(matrix):
            for j, char in enumerate(row):
                new_matrix[i][j] = self.sound2idx.get(char, 0)
        out = [self.idx2sound.get(idx, unknown) for idx in
                self.classifiers[target].predict(new_matrix)]
        return [x for x in out if x != "-"]


def predict_words(ifile, pfile, ofile):

    bs = Baseline(ifile)
    bs.fit()
    languages, sounds, testdata = load_cognate_file(pfile)
    predictions = defaultdict(dict)
    for cogid, values in progressbar(testdata.items(), desc="predicting words"):
        alms, current_languages = [], []
        target = ""
        for language in languages:
            if language in values and " ".join(values[language]) not in ["?", ""]:
                alms += [values[language]]
                current_languages += [language]
            elif " ".join(values[language]) == "?":
                target = language

        if alms and target:
            out = bs.predict(current_languages, alms, target)
            predictions[cogid][target] = out
    write_cognate_file(bs.languages, predictions, ofile)


def compare_words(firstfile, secondfile, report=True):
    """
    Evaluate the predicted and attested words in two datasets.
    """

    (languages, soundsA, first), (languagesB, soundsB, last) = load_cognate_file(firstfile), load_cognate_file(secondfile)
    all_scores = []
    for language in languages:
        scores = []
        almsA, almsB = [], []
        for key in first:
            if language in first[key]:
                entryA = first[key][language]
                if " ".join(entryA):
                    entryB = last[key][language]
                    almA, almB, _ = nw_align(entryA, entryB)
                    almsA += almA
                    almsB += almB
                    score = 0
                    for a, b in zip(almA, almB):
                        if a == b and a not in "Ø?-":
                            pass
                        elif a != b:
                            score += 1
                    scoreD = score / len(almA)
                    bleu = bleu_score(entryA, entryB, n=4, trim=False)
                    scores += [[key, entryA, entryB, score, scoreD, bleu]]
        if scores:
            p, r = bcubed_score(almsA, almsB), bcubed_score(almsB, almsA)
            fs = 2 * (p*r) / (p+r)
            all_scores += [[
                language,
                sum([row[-3] for row in scores])/len(scores),
                sum([row[-2] for row in scores])/len(scores),
                fs,
                sum([row[-1] for row in scores])/len(scores)]]
    all_scores += [[
        "TOTAL", 
        sum([row[-4] for row in all_scores])/len(languages),
        sum([row[-3] for row in all_scores])/len(languages),
        sum([row[-2] for row in all_scores])/len(languages),
        sum([row[-1] for row in all_scores])/len(languages),
        ]]
    if report:
        print(
                tabulate(
                    all_scores, 
                    headers=[
                        "Language", "ED", "ED (Normalized)", 
                        "B-Cubed FS", "BLEU"], floatfmt=".3f"))
    return all_scores
    

def main(*args):

    parser = argparse.ArgumentParser(description='ST 2022')
    parser.add_argument(
            "--download", 
            action="store_true",
            help="Download data via GIT."
            )
    parser.add_argument(
            "--datapath",
            default=Path("data"),
            type=Path,
            action="store",
            help="Folder containing the data for training."
            )
    parser.add_argument(
            "--cldf-data",
            default=Path("cldf-data"),
            type=Path,
            action="store",
            help="Folder containing cldf-data."
            )
    parser.add_argument(
            "--prepare",
            action="store_true",
            help="Prepare data by conducting cognate judgments."
            )
    parser.add_argument(
            "--split",
            action="store_true",
            help="Split data into test and training data."
            )
    parser.add_argument(
            "--runs",
            action="store",
            type=int,
            default=10000,
            help="Iterations for cognate detection analysis (default=10000)."
            )
    parser.add_argument(
            "--seed",
            action="store_true",
            help="Our standard random seed. If set, will set the seed to 1234."
            )
    parser.add_argument(
            "--predict",
            action="store_true",
            help="Predict words with the baseline."
            )
    parser.add_argument(
            "--infile",
            action="store",
            type=Path,
            help="File which will be analyzed."
            )
    parser.add_argument(
            "--outfile",
            action="store",
            default="",
            help="File to which results of baseline will be written."
            )
    parser.add_argument(
            "--testfile",
            action="store",
            default="",
            help="File containing the test data."
            )
    parser.add_argument(
            "--prediction-file",
            action="store",
            default="",
            help="File storing the predictions."
            )
    parser.add_argument(
            "--solution-file",
            action="store",
            default="",
            help="File storing the solutions for a test."
            )
    parser.add_argument(
            "--compare",
            help="Compare two individual datasets.",
            action="store_true"
            )

    parser.add_argument(
            "--datasets",
            action="store",
            default="datasets.json",
            help="Path to the JSON file with the datasets (default=datasets.json)."
            )

    parser.add_argument(
            "--all",
            action="store_true",
            help="Flag indicates if all datasets should be analyzed."
            )

    parser.add_argument(
            "--evaluate",
            action="store_true",
            help="Evaluate results by comparing two files."
            )

    parser.add_argument(
            "--proportion",
            action="store",
            type=float,
            default=0.2,
            help="Define the proportion of test data to analyze with the baseline."
            )
    parser.add_argument(
            "--test-path",
            action="store",
            default=None,
            help="Provide path to the test data for a given system"
            )

    args = parser.parse_args(*args)
    if args.seed:
        random.seed(1234)
    
    with open(args.datasets) as f:
        DATASETS = json.load(f)


    if args.download:
        download(DATASETS, args.cldf_data)
    
    if args.prepare:
        prepare(DATASETS, args.datapath, args.cldf_data, args.runs)
    
    if args.split:
        split_data(DATASETS, args.datapath, props=None)


    if args.predict:
        prop = "{0:.2f}".format(args.proportion)
        if not args.all:
            if not args.outfile:
                args.outfile = Path(str(args.infile)[:-4]+"-out.tsv")
            predict_words(args.infile, args.testfile, args.outfile)
        elif args.all:
            for data, conditions in DATASETS.items():
                print("[i] analyzing {0}".format(data))
                predict_words(
                        args.datapath.joinpath(data, "training-"+prop+".tsv"),
                        args.datapath.joinpath(data, "test-"+prop+".tsv"),
                        args.datapath.joinpath(data, "result-"+prop+".tsv")
                        )
    if args.evaluate:
        prop = "{0:.2f}".format(args.proportion)
        if args.all:
            if not args.test_path:
                pth = args.datapath
            else:
                pth = Path(args.test_path)
            results = []
            for data, conditions in DATASETS.items():
                results += [compare_words(
                        pth.joinpath(data, "result-"+prop+".tsv"),
                        args.datapath.joinpath(data,
                            "solutions-"+prop+".tsv"),
                        report=False)[-1]]
                results[-1][0] = data
            print(tabulate(sorted(results), headers=[
                "DATASET", "ED", "ED (NORM)", "B-CUBED FS", "BLEU"], floatfmt=".3f"))


    if args.compare:
        compare_words(args.prediction_file, args.solution_file)

