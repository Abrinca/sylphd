"""The static catalogues: pre-built sylph databases and sylph-tax taxonomy files.

Pure data (sizes, MD5 sums, mirrors) that :mod:`sylphd.download` verifies
transfers against; the lookup, listing and transfer code lives there.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# database catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Database:
    """A pre-built sylph database and where to get it.

    ``taxonomy`` is the sylph-tax catalogue name (``TAXONOMIES``) whose file
    goes with the genomes of this database; ``taxonomy_alternatives`` are
    further sources a user may pick instead (UHGV ships a default and an ICTV
    lineage).  Both are empty when no file is published.
    """

    name: str
    filename: str
    mirrors: tuple[str, ...]
    size: int | None = None
    md5: str | None = None
    genomes: int | None = None
    c: int | None = None
    description: str = ""
    taxonomy: str | None = None
    taxonomy_alternatives: tuple[str, ...] = ()


_FAUST = "http://faust.compbio.cs.cmu.edu/sylph-stuff/"
_GCS = "https://storage.googleapis.com/sylph-stuff/"


def _hosted(
    name: str, *, hosts: tuple[str, ...] = (_FAUST, _GCS), **fields: object
) -> Database:
    """A database published as ``name`` on each of ``hosts``."""
    return Database(
        name=name,
        filename=name,
        mirrors=tuple(host + name for host in hosts),
        **fields,  # type: ignore[arg-type]
    )


#: Pre-built databases from the sylph project, see
#: https://sylph-docs.github.io/pre%E2%80%90built-databases/.
#:
#: Sizes were recorded from the servers' ``Content-Length``. MD5 sums are the
#: object checksums published by the Google Cloud mirror (``x-goog-hash``), or
#: were computed from a local copy where noted; mean "that mirror has no
#: published checksum" (multipart uploads), so ``--verify`` cannot check them.
DATABASES: dict[str, Database] = {
    database.name: database
    for database in (
        # -- GTDB ------------------------------------------------------
        _hosted(
            "gtdb-r232-c200-dbv2.syl2db",
            size=20044737329,
            md5="89ad2dbdd532be81c72e78f5d493d78d",
            genomes=199923,
            c=200,
            description="GTDB r232 species representatives, two-stage (recommended)",
            taxonomy="GTDB_r232",
        ),
        _hosted(
            "gtdb-r232-c200-dbv1.syldb",
            size=25872594195,
            md5="051e7c7debff82b367dc2073965506e5",
            genomes=199923,
            c=200,
            description="GTDB r232 species representatives, c200",
            taxonomy="GTDB_r232",
        ),
        _hosted(
            "gtdb-r232-c1000-dbv1.syldb",
            size=5219051219,
            md5="8cc9bc3ca5eac9ab7b57527a312fd623",
            genomes=199923,
            c=1000,
            description="GTDB r232 species representatives, c1000",
            taxonomy="GTDB_r232",
        ),
        _hosted(
            "gtdb-r226-c200-dbv2.syl2db",
            size=14272284891,
            md5="1815389774987c7ce3d713c975a8853f",
            genomes=143614,
            c=200,
            description="GTDB r226 species representatives, two-stage",
            taxonomy="GTDB_r226",
        ),
        _hosted(
            "gtdb-r226-c200-dbv1.syldb",
            size=18414135368,
            md5="a4ef042a2a5b037a276a5922c150d5d3",
            genomes=143614,
            c=200,
            description="GTDB r226 species representatives, c200",
            taxonomy="GTDB_r226",
        ),
        _hosted(
            "gtdb-r226-c1000-dbv1.syldb",
            size=3713953632,
            md5="e56ac73c24e0e77f7e72a6e25a08c6a5",
            genomes=143614,
            c=1000,
            description="GTDB r226 species representatives, c1000",
            taxonomy="GTDB_r226",
        ),
        _hosted(
            "gtdb-r220-c200-dbv1.syldb",
            size=14061572967,
            genomes=113104,
            c=200,
            description="GTDB r220 species representatives, c200",
            taxonomy="GTDB_r220",
        ),
        _hosted(
            "gtdb-r220-c1000-dbv1.syldb",
            size=2836800847,
            genomes=113104,
            c=1000,
            description="GTDB r220 species representatives, c1000",
            taxonomy="GTDB_r220",
        ),
        _hosted(
            "v0.3-c200-gtdb-r214.syldb",
            size=10825539803,
            genomes=85202,
            c=200,
            description="GTDB r214 species representatives, c200",
            taxonomy="GTDB_r214",
        ),
        _hosted(
            "v0.3-c1000-gtdb-r214.syldb",
            size=2183213283,
            genomes=85202,
            c=1000,
            description="GTDB r214 species representatives, c1000",
            taxonomy="GTDB_r214",
        ),
        # -- other prokaryotes -----------------------------------------
        _hosted(
            "globdb_r232_sylph_v2_c200.syl2db",
            hosts=(_FAUST,),
            size=31194509116,
            genomes=346233,
            c=200,
            description="GlobDB r232, two-stage (no Google mirror)",
            taxonomy="GlobDB_r232",
        ),
        _hosted(
            "OceanDNA-c200-v0.3.syldb",
            size=827489083,
            genomes=8466,
            c=200,
            description="OceanDNA ocean MAGs",
            taxonomy="OceanDNA",
        ),
        _hosted(
            "SMAG-c200-v0.3.syldb",
            size=2720077433,
            genomes=21077,
            c=200,
            description="SMAG soil MAGs",
            taxonomy="SoilSMAG",
        ),
        _hosted(
            "uhgg_all_c200_v0.3.0.syldb",
            size=27971290837,
            genomes=289232,
            c=200,
            description="UHGG v2.0.1 gut genomes, not dereplicated (not for profiling)",
        ),
        # -- viruses ---------------------------------------------------
        _hosted(
            "uhgv_c100_dbv1.syldb",
            size=435767920,
            md5="36aa5391c50732f515a603adad9c2e19",
            genomes=171338,
            c=100,
            description="UHGV gut vOTUs, c100",
            taxonomy="UHGV_default",
            taxonomy_alternatives=("UHGV_ictv",),
        ),
        _hosted(
            "uhgv_c200_dbv1.syldb",
            size=227714056,
            md5="2fa195f73b7e685ac19d44f2d56dde1b",
            # measured from the file: len(Database), MD5 matches the entry
            genomes=168536,
            c=200,
            description="UHGV gut vOTUs, c200",
            taxonomy="UHGV_default",
            taxonomy_alternatives=("UHGV_ictv",),
        ),
        _hosted(
            "imgvr_c200_v0.3.0.syldb",
            size=2134450877,
            genomes=2917516,
            c=200,
            description="IMG/VR4.1 high-confidence vOTU representatives",
            taxonomy="IMGVR_4.1",
        ),
        # -- eukaryotes ------------------------------------------------
        _hosted(
            "fungi-refseq-2025-10-11-c200-dbv1.syldb",
            size=783659265,
            md5="954d655c282c6b71df5675f113ada5ca",
            genomes=661,
            c=200,
            description="RefSeq fungi representatives (2025-10-11)",
            taxonomy="FungiRefSeq-latest",
        ),
        _hosted(
            "tara-eukmags-c200-v0.3.syldb",
            size=971458802,
            genomes=713,
            c=200,
            description="TARA Oceans eukaryotic MAGs/SAGs",
            taxonomy="TaraEukaryoticSMAG",
        ),
    )
}


# ---------------------------------------------------------------------------
# taxonomy catalogue (sylph-tax files)
# ---------------------------------------------------------------------------

#: Version of sylph-tax whose file list, file formats and key-probing rules
#: this catalogue was taken from (``sylph_tax/metadata_files.py``, tag v1.9.2,
#: commit d14f286).  Recorded in the metadata file by ``setup``.
SYLPHTAX_VERSION = "1.9.2"

_ZENODO_FILE = "https://zenodo.org/records/{record}/files/{filename}"
ZENODO_API = "https://zenodo.org/api/records/{record}"
_GLOBD_R232 = (
    "https://fileshare.lisc.univie.ac.at/globdb/globdb_r232/globdb_r232_taxonomy.tsv.gz"
)


@dataclass(frozen=True)
class Taxonomy:
    """A sylph-tax taxonomy file and where to get it.

    ``size``/``md5`` are recorded at release time (Zenodo publishes both per
    file); both are verified on download, so a taxonomy file is never used
    unverified.  ``rows`` is the row count of the published file when it is
    known, ``None`` otherwise.

    ``size_only`` marks the single entry whose publisher publishes no checksum
    (GlobDB r232 on the university fileshare).  Every Zenodo entry must end up
    with an MD5 — from the catalogue or from the Zenodo API — because a size
    alone cannot tell the published file from a corrupted or replaced one;
    ``download_taxonomy`` refuses an entry with neither.
    """

    name: str
    filename: str
    mirrors: tuple[str, ...]
    zenodo: int | None = None
    size: int | None = None
    md5: str | None = None
    rows: int | None = None
    description: str = ""
    size_only: bool = False


def _taxonomy(
    name: str,
    filename: str,
    *,
    zenodo: int | None = None,
    mirror: str | None = None,
    size: int | None = None,
    md5: str | None = None,
    rows: int | None = None,
    description: str = "",
    size_only: bool = False,
) -> Taxonomy:
    if mirror is not None:
        mirrors = (mirror,)
    else:
        assert zenodo is not None
        mirrors = (_ZENODO_FILE.format(record=zenodo, filename=filename),)
    return Taxonomy(
        name=name,
        filename=filename,
        mirrors=mirrors,
        zenodo=zenodo,
        size=size,
        md5=md5,
        rows=rows,
        description=description,
        size_only=size_only,
    )


#: sylph-tax's published taxonomy files, by sylph-tax name.  Sizes and MD5
#: sums were read from the Zenodo API (``/api/records/<id>``) and are checked
#: on every download; the GlobDB r232 host publishes no checksum, so only its
#: size is checked.
TAXONOMIES: dict[str, Taxonomy] = {
    taxonomy.name: taxonomy
    for taxonomy in (
        _taxonomy(
            "GTDB_r232",
            "gtdb_r232_metadata.tsv.gz",
            zenodo=19646381,
            size=5474219,
            md5="0b0732e06de59abb69a135ba6faa8220",
            rows=199923,
            description="GTDB r232 species representatives (same rows as -m, no NCBI columns)",
        ),
        _taxonomy(
            "GTDB_r226",
            "gtdb_r226_metadata.tsv.gz",
            zenodo=15314244,
            size=7789225,
            md5="2df8a49c8a8685cb28b057a39c4a78b1",
            description="GTDB r226 species representatives",
        ),
        _taxonomy(
            "GTDB_r220",
            "gtdb_r220_metadata.tsv.gz",
            zenodo=14320496,
            size=12587613,
            md5="ad14ff1636ad93506da1f46b65494fb8",
            description="GTDB r220 species representatives",
        ),
        _taxonomy(
            "GTDB_r214",
            "gtdb_r214_metadata.tsv.gz",
            zenodo=14320496,
            size=6511799,
            md5="52905702b85971795cfd432649ca1e65",
            description="GTDB r214 species representatives",
        ),
        _taxonomy(
            "GlobDB_r232",
            "globdb_r232_taxonomy.tsv.gz",
            mirror=_GLOBD_R232,
            size=8071276,
            rows=346233,
            size_only=True,
            description="GlobDB r232 (fileshare, no published MD5)",
        ),
        _taxonomy(
            "GlobDB_r226",
            "globdb_r226_sylph_tax.tsv.gz",
            zenodo=16322879,
            size=5880768,
            md5="ee4a813fa71c1d22b3830844646974d2",
            description="GlobDB r226",
        ),
        _taxonomy(
            "OceanDNA",
            "ocean_dna_metadata.tsv.gz",
            zenodo=14320496,
            size=245487,
            md5="b0aff88193deefcb5095624fe32bec19",
            description="OceanDNA ocean MAGs",
        ),
        _taxonomy(
            "SoilSMAG",
            "soil_smag_metadata.tsv.gz",
            zenodo=14320496,
            size=432433,
            md5="7a31377f2322c843112d60be79ff473d",
            description="SoilSMAG soil MAGs",
        ),
        _taxonomy(
            "TaraEukaryoticSMAG",
            "tara_SMAGs_metadata.tsv.gz",
            zenodo=14320496,
            size=11837,
            md5="9994fcbe6e48a31211c119cadb9305c1",
            description="Tara Oceans eukaryotic MAGs/SAGs",
        ),
        _taxonomy(
            "IMGVR_4.1",
            "IMGVR_4.1_metadata.tsv.gz",
            zenodo=14320496,
            size=27474934,
            md5="9bf87e0ed0624f6700dfe9c4aa9a0c70",
            rows=5576197,
            description="IMG/VR 4.1 (has a header row and virus hosts)",
        ),
        _taxonomy(
            "UHGV_default",
            "uhgv_default_metadata.tsv.gz",
            zenodo=15670321,
            size=4153567,
            md5="cac9b0e8f34178c01ca73a73590c5182",
            description="UHGV viral clusters, default lineage (with virus hosts)",
        ),
        _taxonomy(
            "UHGV_ictv",
            "uhgv_ictv_metadata.tsv.gz",
            zenodo=15670321,
            size=2482003,
            md5="0a1922b612fb7aa1c353e652c001a546",
            description="UHGV viral clusters, ICTV lineage (with virus hosts)",
        ),
        _taxonomy(
            "FungiRefSeq-latest",
            "fungi_refseq_2025-10-11_metadata.tsv.gz",
            zenodo=17330476,
            size=16698,
            md5="526eef5b1868f856d90cd71cd7ce7830",
            rows=661,
            description="RefSeq fungi representatives (2025-10-11)",
        ),
        _taxonomy(
            "FungiRefSeq-2024-07-25",
            "fungi_refseq_2024-07-25_metadata.tsv.gz",
            zenodo=14320496,
            size=12510,
            md5="b83f431a3c054be1eaebe12f908ac6c1",
            description="RefSeq fungi representatives (2024-07-25)",
        ),
    )
}
