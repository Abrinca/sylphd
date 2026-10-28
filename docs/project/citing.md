# Citing

You are using sylphd. If it helps your research, please cite the chain of work it builds on:

- **sylphd**: the server, the client and the web page. Thomas Roder at
  [Abrinca Genomics](https://abrinca.com).
  [GitHub](https://github.com/Abrinca/sylphd), [Docs](https://abrinca.github.io/sylphd/docs/)
- **pysylph**: Python bindings used by the server. Martin Larralde. PyO3 bindings and Python
  interface to sylph.
  [GitHub](https://github.com/althonos/pysylph), [Docs](https://pysylph.readthedocs.io/)
- **sylph**: the method. Jim Shaw and Yun William Yu. Rapid species-level metagenome profiling and
  containment estimation with sylph (2024). *Nature Biotechnology*.
  [doi:10.1038/s41587-024-02412-y](https://doi.org/10.1038/s41587-024-02412-y).
  [GitHub](https://github.com/bluenote-1577/sylph), [Docs](https://sylph-docs.github.io/)
- **GTDB**: taxonomy and genome metadata (<https://gtdb.ecogenomic.org/>), as GTDB recommends:
    - Parks DH, Chaumeil P-A, Mussig AJ, Rinke C, Chuvochina M, Hugenholtz P. GTDB release 10: a
      complete and systematic taxonomy for 715 230 bacterial and 17 245 archaeal genomes (2025).
      *Nucleic Acids Research*.
      [doi:10.1093/nar/gkaf1040](https://doi.org/10.1093/nar/gkaf1040)
    - Parks DH, Chuvochina M, Rinke C, Mussig AJ, Chaumeil P-A, Hugenholtz P. GTDB: an ongoing
      census of bacterial and archaeal diversity through a phylogenetically consistent, rank
      normalized and complete genome-based taxonomy (2022). *Nucleic Acids Research* 50(D1):D785-D794.
      [doi:10.1093/nar/gkab776](https://doi.org/10.1093/nar/gkab776)

The [web app](https://abrinca.github.io/sylphd/) shows the same list at the bottom of the page.

sylph and pysylph are MIT licensed. The test data in `tests/data` comes from pysylph's test suite.
