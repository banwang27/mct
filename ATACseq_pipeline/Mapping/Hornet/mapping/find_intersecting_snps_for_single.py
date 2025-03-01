""" find_intersecting_snps
This is a rewrite of the official WASP code.  It has a more straightforward
design, that is faster and has no maximum window size, but requires loading all
SNPs into memory at once. For reference, 70 million SNPs requires about 10GB of
RAM.
"""
from __future__ import print_function
import sys
import argparse
import gzip
import time
import itertools as it
from collections import defaultdict, Counter
from glob import glob
from os import path
from pysam import AlignmentFile as Samfile

try:
    from functools import reduce
    from operator import mul
except ImportError as exc:
    # We better hope we're in Python 2.
    print(exc)

MAX_SEQS_PER_READ = 32
DRAW_PROGRESS = False

def product(iterable):
    "Returns the product of all items in the iterable"
    return reduce(mul, iterable, 1)

def get_snps(snpdir, chrom_only = None):
    """Get SNPs from a single file or directory of files
    Returns a dictionary of dictionaries:
    snp_dict = {
                'chrom1' : {
                            pos1 : [ref, alt],
                            pos2 : [ref, alt],
                           },
                'chrom2' : {...},
                ...
                }
    where positions are 0-based
    """
    snp_dict = defaultdict(dict)
    if path.exists(path.join(snpdir, 'all.txt.gz')):
        print(time.strftime(("%b %d ") + time.strftime("%I:%M:%S")),\
         ".... Loading snps from consolidated file.")
        for line in gzip.open(path.join(snpdir, 'all.txt.gz'), 'rt', encoding='ascii'):
            chrom, pos, ref, alt = line.split()
            if chrom_only is not None and chrom != chrom_only:
                continue
            pos = int(pos) - 1
            snp_dict[chrom][pos] = "".join([ref, alt])
        return snp_dict
    for fname in glob(path.join(snpdir, '*.txt.gz')):
        chrom = path.basename(fname).split('.snps.txt.gz')[0]
        if chrom_only is not None and chrom != chrom_only:
            continue
        print(time.strftime(("%b %d ") + time.strftime("%I:%M:%S")), \
            ".... Loading snps from", fname)
        i = -1
        for i, line in enumerate(gzip.open(fname, 'rt', encoding='ascii')):
            pos, ref, alt = line.split()
            pos = int(pos) - 1
            snp_dict[chrom][pos] = "".join([ref, alt])
    if len(snp_dict) == 0:
        print("ERROR: Could not find appropriate SNP files")
        sys.exit(1)
    return snp_dict

def get_indels(snp_dict):
    """Returns a dict-of-dicts with positions of indels
    """
    indel_dict = defaultdict(dict)
    for chrom in snp_dict:
        for pos, alleles in snp_dict[chrom].items():
            if ('-' in alleles) or (max(len(i) for i in alleles) > 1):
                indel_dict[chrom][pos] = True
    return indel_dict

RC_TABLE = {
    ord('A'):ord('T'),
    ord('T'):ord('A'),
    ord('C'):ord('G'),
    ord('G'):ord('C'),
}

def reverse_complement(seq):
    "Reverse complements the string input"
    return seq.translate(RC_TABLE)[::-1]


def get_dual_read_seqs(read1, read2, snp_dict, indel_dict, dispositions,
                       phased=False):
    """ For each pair of reads, get all concordant SNP substitutions
    Note that if the reads overlap, the matching positions in read1 and read2
    will get the same subsitution as each other.
    """
    if read1.is_unmapped or read2.is_unmapped:
        dispositions['unmapped read'] += 1
        return [[], []]

    chrom = read1.reference_name
    snps = {}
    read_posns = defaultdict(lambda: [None, None])


    for (read_pos1, ref_pos) in read1.get_aligned_pairs(matches_only=True):
        if indel_dict[chrom].get(ref_pos, False):
            dispositions['toss_indel'] += 1
            return [[], []]
        if ref_pos in snp_dict[chrom]:
            snps[ref_pos] = snp_dict[chrom][ref_pos]
            read_posns[ref_pos][0] = read_pos1

    for (read_pos2, ref_pos) in read2.get_aligned_pairs(matches_only=True):
        if indel_dict[chrom].get(ref_pos, False):
            dispositions['toss_indel'] += 1
            return [[], []]
        if ref_pos in snp_dict[chrom]:
            snps[ref_pos] = snp_dict[chrom][ref_pos]
            read_posns[ref_pos][1] = read_pos2

    if product(len(i) for i in snps.values()) > MAX_SEQS_PER_READ:
        dispositions['toss_manysnps'] += 1
        return [[], []]

    seq1 = read1.seq
    seq2 = read2.seq

    if len(snps) == 0:
        dispositions['no_snps'] += 1
        return [[seq1], [seq2]]
    num_alleles = len(next(iter(snps.values())))
    if num_alleles > 2:
        # This happens if the SNP dict has multiple rows with the same position
        # so we just toss the read.
        dispositions['toss_manysnps'] += 1
        return [[], []]
        #  raise NotImplementedError("We can't yet do multiple phased genomes")

    if phased:
        reads1 = [[seq1], []]
        reads2 = [[seq2], []]
        last_pos1 = 0
        last_pos2 = 0
        for ref_pos in sorted(snps):
            pos1, pos2 = read_posns[ref_pos]
            alleles = snps[ref_pos]
            if pos1 is not None:
                is_alt = alleles.find(seq1[pos1])
                if is_alt == -1:
                    dispositions['non_refalt_base']
                    return [[], []]
                allele = alleles[1-is_alt]
                reads1[1].append(seq1[last_pos1:pos1])
                reads1[1].append(allele)
                last_pos1 = pos1 + 1

            if pos2 is not None:
                is_alt = alleles.find(seq2[pos2])
                if is_alt == -1:
                    dispositions['non_refalt_base']
                    return [[], []]
                allele = alleles[1-is_alt]
                reads2[1].append(seq2[last_pos2:pos2])
                reads2[1].append(allele)
                last_pos2 = pos2 + 1
            if pos1 is not None and pos2 is not None and seq1[pos1] != seq2[pos2]:
                dispositions['toss_anomalous_phase'] += 1
                return [[], []]

        reads1[1].append(seq1[last_pos1:])
        reads2[1].append(seq2[last_pos2:])
        if len(snps) == 0:
            dispositions['no_snps'] += 1
        else:
            dispositions['has_snps'] += 1
        return [[''.join(r) for r in reads1], [''.join(r) for r in reads2]]


    seqs1, seqs2 = [read1.seq], [read2.seq]


    for ref_pos in snps:
        match = False
        dispositions['total_snps'] += 1
        alleles = snps[ref_pos]
        pos1, pos2 = read_posns[ref_pos]
        new_seqs1 = []
        new_seqs2 = []
        if pos1 is None:
            if seq2[pos2] in alleles:
                dispositions['ref_match'] += 1
            else:
                dispositions['no_match'] += 1

            for allele in alleles:
                if allele == seq2[pos2]:
                    continue
                for seq1, seq2 in zip(seqs1, seqs2):
                    new_seqs1.append(seq1)
                    new_seqs2.append(''.join([seq2[:pos2], allele, seq2[pos2+1:]]))

        elif pos2 is None:
            if seq1[pos1] in alleles:
                dispositions['ref_match'] += 1
            else:
                dispositions['no_match'] += 1

            for allele in alleles:
                if allele == seq1[pos1]:
                    continue
                for seq1, seq2 in zip(seqs1, seqs2):
                    new_seqs1.append(''.join([seq1[:pos1], allele, seq1[pos1+1:]]))
                    new_seqs2.append(seq2)
        else:
            if seq1[pos1] != seq2[pos2]:
                dispositions['toss_anomalous_phase'] += 1
                return [[], []]
            if seq2[pos2] in alleles:
                dispositions['ref_match'] += 1
            else:
                dispositions['no_match'] += 1
            for allele in alleles:
                if allele == seq2[pos2]:
                    continue
                for seq1, seq2 in zip(seqs1, seqs2):
                    new_seqs1.append(''.join([seq1[:pos1], allele, seq1[pos1+1:]]))
                    new_seqs2.append(''.join([seq2[:pos2], allele, seq2[pos2+1:]]))
        seqs1.extend(new_seqs1)
        seqs2.extend(new_seqs2)

    if len(seqs1) == 1:
        dispositions['no_snps'] += 1
    else:
        dispositions['has_snps'] += 1
    return seqs1, seqs2

def get_read_seqs(read, snp_dict, indel_dict, dispositions, phased=False):
    """ For each read, get all possible SNP substitutions
    for N biallelic snps in the read, will return 2^N reads
    """
    num_snps = 0
    seqs = [read.seq]
    if phased:
        raise NotImplementedError("Not yet implemented. It should work with paired end reads, though")

    chrom = read.reference_name
    for (read_pos, ref_pos) in read.get_aligned_pairs(matches_only=True):
        if ref_pos is None:
            continue
        if indel_dict[chrom].get(ref_pos, False):
            dispositions['toss_indel'] += 1
            return []
        if len(seqs) > MAX_SEQS_PER_READ:
            dispositions['toss_manysnps'] += 1
            return []

        if ref_pos in snp_dict[chrom]:
            dispositions['total_snps'] += 1

            read_base = read.seq[read_pos]
            if read_base in snp_dict[chrom][ref_pos]:
                dispositions['ref_match'] += 1
                num_snps += 1
                for new_allele in snp_dict[chrom][ref_pos]:
                    if new_allele == read_base:
                        continue
                    for seq in list(seqs):
                        # Note that we make a copy up-front to avoid modifying
                        # the list we're iterating over
                        new_seq = seq[:read_pos] + new_allele + seq[read_pos+1:]
                        seqs.append(new_seq)
            else:
                dispositions['no_match'] += 1
        else:
            # No SNP
            pass
    if len(seqs) == 1:
        dispositions['no_snps'] += 1
    else:
        dispositions['has_snps'] += 1
    return seqs

def assign_reads(insam, snp_dict, indel_dict, is_paired=True, phased=False, keep_only=None):
    """ Loop through all the reads in insam and output them to the appropriate file
    """
    fname = insam.filename
    chroms = insam.references
    if isinstance(fname, bytes):
        fname = fname.decode('ascii')
    basename = fname.rsplit('.', 1)[0]
    keep = Samfile('.'.join([basename, 'keep.bam']),
                   'wb',
                   template=insam)
    remap_bam = Samfile('.'.join([basename, 'to.remap.bam']),
                        'wb',
                        template=insam)
    dropped_bam = Samfile('.'.join([basename, 'dropped.bam']),
                          'wb',
                          template=insam)
    if is_paired:
        fastqs = [
            gzip.open('.'.join([basename, 'remap.fq1.gz']), 'wt'),
            gzip.open('.'.join([basename, 'remap.fq2.gz']), 'wt'),
        ]
    else:
        fastqs = [gzip.open('.'.join([basename, 'remap.fq.gz']), 'wt'),]
    unpaired_reads = [{}, {}]
    read_results = Counter()
    remap_num = 1
    global DRAW_PROGRESS
    if DRAW_PROGRESS:
        try:
            from tqdm import tqdm
            iterator = tqdm(enumerate(insam), total=insam.mapped,
                            mininterval=5, unit_scale=True, unit='Rd')
        except:
            try:
                from progressbar import ProgressBar
                iterator = ProgressBar(max_value=insam.mapped)(enumerate(insam))
            except:
                iterator = enumerate(insam)
    else:
        iterator = enumerate(insam)
    for i, read in iterator:
        if keep_only is not None and chroms[read.reference_id] != keep_only:
            read_results['skipped'] += 1
            continue
        read_results['total'] += 1
        if i % 10000 == 0:
            pass
        if not is_paired:
            read_seqs = get_read_seqs(read, snp_dict, indel_dict, read_results,
                                     phased)
            remap_num += write_read_seqs([(read, read_seqs)], keep, remap_bam, fastqs, dropped_bam, remap_num)
        elif read.is_proper_pair:
            slot_self = read.is_read2 # 0 if is_read1, 1 if is_read2
            slot_other = read.is_read1
            if read.qname in unpaired_reads[slot_other]:
                both_reads = [None, None]
                both_reads[slot_self] = read
                both_reads[slot_other] = unpaired_reads[slot_other].pop(read.qname)
                both_seqs = get_dual_read_seqs(both_reads[0], both_reads[1],
                                               snp_dict, indel_dict,
                                               read_results, phased)
                both_read_seqs = list(zip(both_reads, both_seqs))
                remap_num += write_read_seqs(both_read_seqs, keep, remap_bam,
                                             fastqs, dropped_bam, remap_num)
            else:
                unpaired_reads[slot_self][read.qname] = read
        else:
            read_results['not_proper_pair'] += 1
            # Most tools assume reads are paired and do not check IDs. Drop it out.
            continue
    print()
    print(time.strftime(("%b %d ") + time.strftime("%I:%M:%S")), ".... Finished!")
    print()
    print("RUN STATISTICS:")

    if is_paired:
        total_pairs = read_results['total']//2
        print("  Total input reads:", total_pairs, "pairs.")
        print("  Unpaired reads:", len(unpaired_reads[0]) + len(unpaired_reads[1]), "(" + \
            "%.2f" % (((len(unpaired_reads[0]) + len(unpaired_reads[1])) / total_pairs)*100) + "%)")
    else:
        total_pairs = read_results['total']
        print("  Total input reads:", total_pairs)
    if keep_only is not None:
        print("  Reads on skipped chromosomes", read_results['skipped'])

    print("  Reads with no SNPs:", read_results['no_snps'], "(" + "%.2f" % ((read_results\
        ['no_snps'] / total_pairs)*100) + "%)")
    print("  Reads overlapping SNPs:", read_results['has_snps'], "(" + "%.2f" % ((read_results\
        ['has_snps'] / total_pairs)*100) + "%)")

    total_snps = read_results['total_snps']
    print("  Total SNPs covered:", total_snps)
    print("\tReference SNP matches:", read_results['ref_match'], "(" + "%.2f" % ((read_results\
            ['ref_match'] / total_snps)*100) + "%)")
    print("\tNon-reference SNP matches:", read_results['no_match'], "(" + "%.2f" % ((read_results\
            ['no_match'] / total_snps)*100) + "%)")

    print("  Reads dropped [INDEL]:", read_results['toss_indel'], "(" + "%.2f" % ((read_results\
        ['toss_indel'] / total_pairs)*100) + "%)")
    print("  Reads dropped [too many SNPs]:", read_results['toss_manysnps'], "(" + "%.2f" % \
        ((read_results['toss_manysnps'] / total_pairs)*100) + "%)")

    if is_paired:
        print("  Reads dropped [anomalous pair]:", read_results['toss_anomalous_phase'], "(" + \
            "%.2f" % ((read_results['toss_anomalous_phase'] / total_pairs)*100) + "%)")
        print("  Reads dropped [not proper pair]:", read_results['not_proper_pair'], "(" + "%.2f" % \
            ((read_results['not_proper_pair'] / total_pairs)*100) + "%)")

    print()

def write_read_seqs(both_read_seqs, keep, remap_bam, fastqs, dropped=None, remap_num=0):
    """Write the given reads out to the appropriate file
    If there are no SNPs in the read, write it to the BAM file `keep`
    If there are too many SNPs, and `dropped` is provided, write the original
    read out to `dropped`
    Otherwise, output all possible substitutions to the fastqs for remapping,
    as well as a bam file containing the original read.
    """
    reads, seqs = zip(*both_read_seqs)
    assert len(reads) == len(fastqs)
    num_seqs = len(both_read_seqs[0][1])
    if num_seqs == 0:
        if dropped is not None:
            for read in reads:
                dropped.write(read)
            return 0
        else:
            return 0
    elif num_seqs == 1:
        for read, seqs in both_read_seqs:
            keep.write(read)
    else:
        assert len(reads) > 0
        for read in reads:
            remap_bam.write(read)
        left_pos = min(r.pos for r in reads)
        right_pos = max(r.pos for r in reads)
        loc_line = '{}:{}:{}:{}:{}'.format(
            remap_num,
            read.reference_name,
            left_pos,
            right_pos,
            len(seqs[0])-1,
        )

        first = True
        # Some python fanciness to deal with single or paired end reads (or
        # n-ended reads, if such technology ever happens.
        for read_seqs in zip(*seqs):
            if first:
                first = False
                continue
            for seq, read, fastq in zip(read_seqs, reads, fastqs):
                fastq.write(
                    "@{loc_line}\n{seq}\n+{loc_line}\n{qual}\n"
                    .format(
                        loc_line=loc_line,
                        seq=reverse_complement(seq) if read.is_reverse else seq,
                        qual=read.qual)
                    )
        return 1
    return 0



if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--paired_end",
                        action='store_true',
                        dest='is_paired_end', default=False,
                        help=('Indicates that reads are '
                              'paired-end (default is single).'))
    parser.add_argument('-C', '--limit-to-chrom',
                        default=None,
                        help="Limit loading of SNPs to the specified chromosome")

    parser.add_argument("-s", "--sorted",
                        action='store_true', dest='is_sorted', default=False,
                        help=('Indicates that the input bam file'
                              ' is coordinate sorted (default is False)'))
    parser.add_argument('-P', "--phased-snp-data",
                       dest='is_phased', action='store_true', default=False,
                       help=('SNP data is phased---useful if there is a '
                             + 'defined "reference" and "alternate", such as '
                             + 'in  a hybrid. If enabled, will only generate '
                             + '2 reads per input read, rather than 2^N_snps.')
                      )
    parser.add_argument('--progressbar', action='store_true', default=False,
                        help='Show a progress bar if possible')


    parser.add_argument("infile", type=Samfile, help=("Coordinate sorted bam "
                                                      "file."))
    snp_dir_help = ('Directory containing the SNPs segregating within the '
                    'sample in question (which need to be checked for '
                    'mappability issues).  This directory should contain '
                    'sorted files of SNPs separated by chromosome and named: '
                    '<chrname>.snps.txt.gz (where chrname is the name of the '
                    'chromosomes in the bam file). These files should contain '
                    '3 columns: position RefAllele AltAllele')

    parser.add_argument("snp_dir", action='store', help=snp_dir_help)

    options = parser.parse_args()
    DRAW_PROGRESS = False
    if options.progressbar:
        DRAW_PROGRESS = True

    SNP_DICT = get_snps(options.snp_dir, options.limit_to_chrom)
    INDEL_DICT = get_indels(SNP_DICT)

    print(time.strftime(("%b %d ") + time.strftime("%I:%M:%S")), ".... Done with SNPs.")

    assign_reads(options.infile, SNP_DICT, INDEL_DICT, options.is_paired_end,
                 options.is_phased, options.limit_to_chrom)