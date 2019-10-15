#!/usr/bin/env python3
"""
Copyright 2019 Ryan Wick (rrwick@gmail.com)
https://github.com/rrwick/Minipolish

This file is part of Minipolish. Minipolish is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version. Minipolish is distributed
in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
details. You should have received a copy of the GNU General Public License along with Minipolish.
If not, see <http://www.gnu.org/licenses/>.
"""

import argparse
import collections
import pathlib
import random
import subprocess
import tempfile

from .alignment import Alignment
from .assembly_graph import load_gfa
from .help_formatter import MyParser, MyHelpFormatter
from .log import log, section_header, explanation
from .misc import iterate_fastq, get_default_thread_count, count_reads, count_fasta_bases, \
    weighted_average
from .racon import run_racon


__version__ = '0.1.0'


def get_arguments(args):
    parser = MyParser(description='Minipolish', add_help=False, formatter_class=MyHelpFormatter)

    required_args = parser.add_argument_group('Positional arguments')
    required_args.add_argument('reads', type=str,
                               help='Long reads for polishing (FASTA or FASTQ format)')
    required_args.add_argument('assembly', type=str,
                               help='Miniasm assembly to be polished (GFA format)')

    setting_args = parser.add_argument_group('Settings')
    setting_args.add_argument('-t', '--threads', type=int, default=get_default_thread_count(),
                              help='Number of threads to use for alignment and polishing')
    setting_args.add_argument('--rounds', type=int, default=2,
                              help='Number of full Racon polishing rounds')

    other_args = parser.add_argument_group('Other')
    other_args.add_argument('-h', '--help', action='help', default=argparse.SUPPRESS,
                            help='Show this help message and exit')
    other_args.add_argument('--version', action='version',
                            version='Minipolish v' + __version__,
                            help="Show program's version number and exit")

    args = parser.parse_args(args)
    return args


def main(args=None):
    args = get_arguments(args)
    # TODO: check for Racon and minimap2
    random.seed(0)
    graph = load_gfa(args.assembly)
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_dir = pathlib.Path(tmp_dir)
        initial_polish(graph, args.reads, args.threads, tmp_dir)
        if args.rounds > 0:
            full_polish(graph, args.reads, args.threads, args.rounds, tmp_dir)
        assign_depths(graph, args.reads, args.threads, tmp_dir)
    # TODO: redo the link overlaps
    graph.print_to_stdout()


def initial_polish(graph, read_filename, threads, tmp_dir):
    section_header('Initial polishing round')
    explanation('The first round of polishing is done on a per-segment basis and only uses reads '
                'which are definitely associated with the segment (because the GFA indicated that '
                'they were used to make the segment).')
    save_per_segment_reads(graph, read_filename, tmp_dir)
    for segment in list(graph.segments.values()):
        seg_read_filename = tmp_dir / (segment.name + '.fastq')
        seg_seq_filename = tmp_dir / (segment.name + '.fasta')
        segment.save_to_fasta(seg_seq_filename)
        fixed_seqs = run_racon(segment.name, seg_read_filename, seg_seq_filename, threads,
                               tmp_dir)
        fixed_seq = fixed_seqs[segment.name]
        if len(fixed_seq) > 0:
            segment.sequence = fixed_seq
        else:
            graph.remove_segment(segment.name)
            log(f'Removing empty segment: {segment.name}')
    log()


def full_polish(graph, read_filename, threads, rounds, tmp_dir):
    section_header('Full polishing rounds')
    explanation('The assembly graph is now polished using all of the reads. Multiple rounds of '
                'polishing are done, and circular contigs are rotated between rounds.')
    for i in range(rounds):
        round_name = f'round_{i+1}'
        graph.rotate_circular_sequences()
        unpolished_filename = tmp_dir / (round_name + '.fasta')
        graph.save_to_fasta(unpolished_filename)
        fixed_seqs = run_racon(round_name, read_filename, unpolished_filename, threads, tmp_dir)
        graph.replace_sequences(fixed_seqs)
    log()


def assign_depths(graph, read_filename, threads, tmp_dir):
    section_header('Assign read depths')
    explanation('The reads are aligned to the contigs one final time to calculate read depth '
                'values.')
    log(f'Aligning reads:')
    read_count = count_reads(read_filename)
    log(f'  reads:      {read_filename} ({read_count:,} reads)')

    depth_filename = tmp_dir / 'depths.fasta'
    graph.save_to_fasta(depth_filename)
    base_count = count_fasta_bases(depth_filename)
    log(f'  contigs:    {depth_filename} ({base_count:,} bp)')

    command = ['minimap2', '-t', str(threads), '-x', 'map-ont', depth_filename, read_filename]
    alignments_filename = tmp_dir / 'depths.paf'
    minimap2_log = tmp_dir / 'depths_minimap2.log'
    with open(alignments_filename, 'wt') as stdout, open(minimap2_log, 'w') as stderr:
        subprocess.call(command, stdout=stdout, stderr=stderr)

    alignments = []
    with open(alignments_filename, 'rt') as alignments_file:
        for line in alignments_file:
            alignments.append(Alignment(line))
    log(f'  alignments: {alignments_filename} ({len(alignments):,} alignments)')

    depth_per_contig = {name: 0.0 for name in graph.segments.keys()}
    for a in alignments:
        depth_per_contig[a.ref_name] += a.get_ref_depth_contribution()
    graph.set_depths(depth_per_contig)

    segment_names = sorted(graph.segments.keys())
    depths = [depth_per_contig[n] for n in segment_names]
    lengths = [graph.get_segment_length(n) for n in segment_names]
    mean_depth = weighted_average(depths, lengths)
    log(f'  mean depth: {mean_depth:.3f}x')
    log()


def save_per_segment_reads(graph, read_filename, tmp_dir):
    read_to_segment = collections.defaultdict(list)
    for segment in graph.segments.values():
        for read_name in segment.read_names:
            read_to_segment[read_name].append(segment.name)
    for read_name, seq, qual in iterate_fastq(read_filename):
        if read_name not in read_to_segment:
            continue
        for seg_name in read_to_segment[read_name]:
            seg_read_filename = tmp_dir / (seg_name + '.fastq')
            with open(seg_read_filename, 'at') as seg_read_file:
                seg_read_file.write(f'@{read_name}\n{seq}\n+\n{qual}\n')


if __name__ == '__main__':
    main()