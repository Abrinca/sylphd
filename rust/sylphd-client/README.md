# sylphd-bin

The `sylphd-client` binary of [sylphd](https://abrinca.github.io/sylphd/): it sketches
FASTA/FASTQ locally and asks a sylphd server (`profile`, `query`, `assembly`, `info`), or
renders a saved JSON result (`render`).

This wheel only carries the binary (`py3-none-<platform>`, any Python). Install it through
`pip install 'sylphd[client]'`, which also gives you the `sylphd` command that forwards to it,
or download a standalone archive from the GitHub releases.
