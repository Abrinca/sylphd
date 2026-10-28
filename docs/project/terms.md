# Terms of the web service

These terms cover the **public sylphd web service operated by Abrinca Genomics**. They do not
restrict your use of the open-source software, which is licensed under the
[AGPL-3.0-or-later](license.md). The page served with the web app,
[abrinca.github.io/sylphd/terms.html](https://abrinca.github.io/sylphd/terms.html), is the
authoritative version; this page reproduces its content.

In short: the service is free for academic, non-commercial use through the web page, and anything
else needs an agreement with us first.

## Free academic use

The service is provided free of charge for academic, non-commercial research use through the web
page.

## Other uses

Other uses, including programmatic or API access (for example calling the server directly from
scripts or pipelines), commercial use, and high-volume use, are only allowed after an agreement
with us. Please contact us first: <https://abrinca.com/contact>. This applies whether the use is
for a non-profit or a commercial organisation; we are happy to talk about it.

## Your sequences

In the **Assembly** and **Reads** modes of the web page, your sequences stay on your computer: they
are sketched in your browser and only the sketch (k-mer hashes and counts, no sequences) is sent to
the server, processed in memory and not stored or logged.

Only if your browser cannot make the sketch (or the server is an older one), the Assembly mode can
send the assembly itself, where the server allows that. It is then processed in memory to compute
the result; it is not stored and its contents are not logged. The page says which of the two
happens (the privacy line at the top, and *sketched locally* or *sequence uploaded* on the result).

In all cases only sizes, timings and request outcomes may be recorded, to run and monitor the
service.

## No warranty, availability

The service is provided as-is, without warranty of any kind. We do not guarantee availability or
correctness, and we may rate-limit, change or discontinue the service at any time.

## Fair use

Please do not overload the service or use it to disrupt other users. Automated or bulk access
belongs under an agreement, as above.

## Contact

Operator: [Abrinca Genomics](https://abrinca.com). Questions about these terms or about other uses:
<https://abrinca.com/contact>.

## Running your own server

Nothing here applies to a server you run yourself. To tell users of your instance about your own
terms, start it with [`--terms-url`](../commands/serve.md#public-facing-options).
