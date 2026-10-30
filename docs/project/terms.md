# Terms of the web service

These terms cover the **public sylphd web service operated by Abrinca Genomics**. They do not
restrict your use of the open-source software, which is licensed under the
[AGPL-3.0-or-later](license.md). The page served with the web app,
[abrinca.github.io/sylphd/terms.html](https://abrinca.github.io/sylphd/terms.html), is the
authoritative version; this is a summary.

- The service is free for academic, non-commercial use through the web page and, for regular light
  use (your own samples, scripts and pipelines), through the API.
- Commercial use and heavy or bulk programmatic use (also if you regularly hit the rate limit)
  need an agreement with us first.
- Through the API you can send a sketch (preferred) or a raw FASTA/FASTQ file to `/assembly`. The
  server sketches raw sequences in memory and neither stores nor logs them, but unlike a sketch
  they do reach the server. The web page sketches in your browser, so your sequences stay on your
  computer.
- The service may keep [usage statistics](../guides/public-server.md#usage-statistics) (it says so
  in the status line of the page): counts of successful requests per ten-minute window, endpoint and
  kind of client, with sizes and timings. They never contain IP addresses, names, results or
  anything per person; the web page asks you to agree before your first upload, and the request log
  of the service holds no IP address either.
- The service is provided as-is, without warranty, and may be rate-limited, changed or discontinued.
- Contact: <https://abrinca.com/contact>.

Nothing here applies to a server you run yourself. To tell users of your instance about your own
terms, put them into the plain text of its [`--about`](../guides/public-server.md#describing-your-instance) file.
