# What does "same species" mean?

sylphd tells you which GTDB genomes are closest to your assembly and, when the
best hit is close enough, which species your assembly belongs to. This page
explains what that call is based on, where it is solid, and where it needs
care. It is background for interpreting results, not a taxonomic ruling.

## How sylphd calls a species

- sylphd compares your assembly to every genome in the database with sylph and
  reports a **k-mer ANI** for each hit.
- If the best hit reaches **95 % ANI**, your assembly is assigned to that hit's
  GTDB species. Otherwise there is no species-level match, and sylphd shows the
  closest relative instead.
- The relation of every other hit ("same genus", "same family", ...) comes
  from the GTDB lineage, not from the ANI value.

The 95 % cutoff is a convention, but a well-supported one. Early work showed
that the classical species criterion of 70 % DNA-DNA hybridization corresponds
closely to about 94–95 % ANI.[^1][^2][^3] Large genome surveys later found that
genome pairs with ANI between roughly 83 % and 95 % are scarce, so cultured and
uncultured diversity falls into sequence-discrete clusters that match species
well.[^4][^5]

## ANI is not one number

"ANI" names a family of methods (BLAST-based ANIb, MUMmer-based ANIm,
OrthoANI, FastANI, gANI and others), and they do not give the same value for
the same genome pair. They differ in how genomes are fragmented, how homologous
regions are found, and which regions are discarded. Methods differ across the
whole range, and their disagreement matters most between 90 % and 100 %, which
includes the species boundary.[^6]

sylphd reports **sylph's k-mer ANI**, estimated from shared k-mers, using the
naive (unadjusted) containment ANI because an assembly has no read coverage to
correct for. Therefore:

- Do not compare a sylphd ANI directly with an ANIb or FastANI value from
  another tool or paper.
- Below about 85 % the k-mer ANI becomes imprecise. The gauge marks this region
  and it never changes an assignment.
- The **containment** column (shared k-mers / query k-mers) is a separate
  signal from ANI. How much of the genome can be matched varies between
  methods and between genome pairs,[^6]
  so read it as a hint about completeness and
  relatedness, not as a score.

## Species-boundary cases

Sometimes the result card says that another hit reaches 95 % ANI but belongs to
a different GTDB species. This happens, and it is expected from the science,
not a bug.

GTDB assigns genomes to species clusters around representative genomes, using
ANI together with an alignment-fraction criterion inside a curated taxonomy.[^7]
Because sylphd compares your assembly with every genome in the database, a query
can sit in one GTDB species and still reach 95 % ANI with a genome that GTDB
places in a neighbouring species. In well-studied groups some accepted species
also have within-species ANI below 95 %, while some pairs of accepted species
are more similar than that.[^6] The cutoff should be read as a guideline:
species are products of their own evolutionary histories, and a single
universal value cannot fit all of them.[^6][^8]

What to do with such a case:

1. Look at both candidate species and their ANI values side by side.
2. Check the containment fraction and the genome quality of the hits.
3. For anything that matters (a species description, a publication, a clinical
   decision), confirm with a phylogenomic analysis and other evidence, such as
   phenotype or ecology.

## Species can be old and diverse

A bacterial species is not a tight clone. Genomes of one species can differ in
a substantial part of their gene content: up to about 35 %, or roughly 20 %
when hypothetical and mobile genes are excluded, which can mean on the order of
a thousand functional genes in a 5 Mb genome.[^3]

*Escherichia coli* illustrates this well. The 97 % core genome of 10,667 *E.
coli* genomes represents only 1.96 % of the total pangenome,[^9] and the last common ancestor of *E. coli*
K-12 and *E. coli* O157:H7 lived about 4.5 million years ago.[^10] Two genomes at 98 % ANI can therefore be
very different organisms in terms of gene content.

## Why species stay together

If lineages are this old, why do they not drift apart into a continuum? Recent
work on *Salinibacter ruber* and *E. coli* suggests two ingredients that work
together:

- **Ecological cohesiveness:** members of a species share a niche, so they
  meet each other.
- **Frequent homologous recombination** that is spread across the genome and
  not driven by selection on a few loci. Within a species it removes more
  differences than point mutation creates (a recombination-to-mutation ratio
  often around 3 to 5), while recombination between species is far rarer.[^5]

One consequence is that ANI is not a molecular clock. Recombination keeps
resetting divergence, so ANI between genomes says less about elapsed time than
one might expect.

## Same species is not the same phenotype

An ANI-based species call says how related two genomes are overall. It does not
guarantee that they behave alike.

- *Burkholderia mallei* and *B. pseudomallei* share 98.7 % ANI, yet *B. mallei*
  lost about 1.7 Mb of genome as it became an obligate pathogen.[^3]
- *Salmonella* strains above 98.5 % ANI include pathovars with different
  pathogenicity.[^3]

Clinically or legally relevant names (pathovars, biovars, regulated organisms)
often do not follow the ANI-based species boundary. If the species name is
used for something with consequences, check the organism-level evidence too.

## Units below the species level

Recent work reports a second gap in the ANI distribution inside species, around
**99.5 % ANI**, which can define intraspecies units called *genomovars*, and
proposes calling genomes with ANI above **99.99 %** the same *strain*.[^11][^5]

sylphd does not call genomovars or strains. Those thresholds were derived with
alignment-based ANI, and a sketch-based k-mer ANI at c=200 is not designed to
resolve differences this small. If you need strain-level resolution, use
dedicated tools on the closest genomes sylphd identifies.

## Reading the ANI value at a glance

These bands are rules of thumb, not definitions. Each range includes its lower
bound and excludes its upper bound.

| sylph ANI | Typical reading |
|---|---|
| ≥ 99.5 % | Very close genomes; possibly the same intraspecies unit, but confirm with alignment-based ANI (see above) |
| 95 % to 99.5 % | Same species by the common cutoff |
| about 85 % to 95 % | Usually a different species, often the same genus; boundary cases possible |
| < 85 % | Distant relative; ANI is imprecise here, so rely on the lineage relation |

## Further reading

- Palmer M, et al. (2020). All ANIs are not created equal: implications for prokaryotic species
  boundaries and integration of ANIs into polyphasic taxonomy.
  [doi:10.1099/ijsem.0.004124](https://doi.org/10.1099/ijsem.0.004124)
- Konstantinidis KT, Ramette A, Tiedje JM (2006). The bacterial species definition in the genomic
  era. [doi:10.1098/rstb.2006.1920](https://doi.org/10.1098/rstb.2006.1920)
- Conrad RE, et al. (2024). Microbial species and intraspecies units exist and are maintained by
  ecological cohesiveness coupled to high homologous recombination.
  [doi:10.1038/s41467-024-53787-0](https://doi.org/10.1038/s41467-024-53787-0)
- Rodriguez-R LM, et al. (2024). An ANI gap within bacterial species that advances the definitions
  of intra-species units. [doi:10.1128/mbio.02696-23](https://doi.org/10.1128/mbio.02696-23)
- Jain C, et al. (2018). High throughput ANI analysis of 90K prokaryotic genomes reveals clear
  species boundaries. [doi:10.1038/s41467-018-07641-9](https://doi.org/10.1038/s41467-018-07641-9)

## References

[^1]: Goris J, Konstantinidis KT, Klappenbach JA, Coenye T, Vandamme P, Tiedje JM. DNA-DNA
    hybridization values and their relationship to whole-genome sequence similarities (2007).
    *International Journal of Systematic and Evolutionary Microbiology* 57(1):81-91.
    [doi:10.1099/ijs.0.64483-0](https://doi.org/10.1099/ijs.0.64483-0)
[^2]: Konstantinidis KT, Tiedje JM. Genomic insights that advance the species definition for
    prokaryotes (2005). *Proceedings of the National Academy of Sciences* 102(7):2567-2572.
    [doi:10.1073/pnas.0409727102](https://doi.org/10.1073/pnas.0409727102)
[^3]: Konstantinidis KT, Ramette A, Tiedje JM. The bacterial species definition in the genomic era
    (2006). *Philosophical Transactions of the Royal Society B* 361(1475):1929-1940.
    [doi:10.1098/rstb.2006.1920](https://doi.org/10.1098/rstb.2006.1920)
[^4]: Jain C, Rodriguez-R LM, Phillippy AM, Konstantinidis KT, Aluru S. High throughput ANI
    analysis of 90K prokaryotic genomes reveals clear species boundaries (2018). *Nature
    Communications* 9(1):5114.
    [doi:10.1038/s41467-018-07641-9](https://doi.org/10.1038/s41467-018-07641-9)
[^5]: Conrad RE, Brink CE, Viver T, Rodriguez-R LM, Aldeguer-Riquelme B, Hatt JK, et al. Microbial
    species and intraspecies units exist and are maintained by ecological cohesiveness coupled to
    high homologous recombination (2024). *Nature Communications* 15(1):9906.
    [doi:10.1038/s41467-024-53787-0](https://doi.org/10.1038/s41467-024-53787-0)
[^6]: Palmer M, Steenkamp ET, Blom J, Hedlund BP, Venter SN. All ANIs are not created equal:
    implications for prokaryotic species boundaries and integration of ANIs into polyphasic
    taxonomy (2020). *International Journal of Systematic and Evolutionary Microbiology*
    70(4):2937-2948. [doi:10.1099/ijsem.0.004124](https://doi.org/10.1099/ijsem.0.004124)
[^7]: Parks DH, Chaumeil P-A, Mussig AJ, Rinke C, Chuvochina M, Hugenholtz P. GTDB release 10: a
    complete and systematic taxonomy for 715 230 bacterial and 17 245 archaeal genomes (2026).
    *Nucleic Acids Research* 54(D1):D743-D754.
    [doi:10.1093/nar/gkaf1040](https://doi.org/10.1093/nar/gkaf1040)
[^8]: Palmer M, Venter SN, Coetzee MPA, Steenkamp ET. Prokaryotic species are sui generis
    evolutionary units (2019). *Systematic and Applied Microbiology* 42(2):145-158.
    [doi:10.1016/j.syapm.2018.10.002](https://doi.org/10.1016/j.syapm.2018.10.002)
[^9]: Abram K, Udaondo Z, Bleker C, Wanchai V, Wassenaar TM, Robeson MS, et al. Mash-based analyses
    of Escherichia coli genomes reveal 14 distinct phylogroups (2021). *Communications Biology*
    4(1):117. [doi:10.1038/s42003-020-01626-5](https://doi.org/10.1038/s42003-020-01626-5)
[^10]: Roder T, Pimentel G, Fuchsmann P, Stern MT, von Ah U, Vergères G, et al. Scoary2: rapid
    association of phenotypic multi-omics data with microbial pan-genomes (2023). *bioRxiv*
    [preprint]. [doi:10.1101/2023.04.19.537353](https://doi.org/10.1101/2023.04.19.537353)
[^11]: Rodriguez-R LM, Conrad RE, Viver T, Feistel DJ, Lindner BG, Venter SN, et al. An ANI gap
    within bacterial species that advances the definitions of intra-species units (2024). *mBio*
    15(1):e02696-23. [doi:10.1128/mbio.02696-23](https://doi.org/10.1128/mbio.02696-23)
