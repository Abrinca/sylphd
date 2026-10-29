# License

sylphd is licensed under the **GNU Affero General Public License, version 3 or later**
(AGPL-3.0-or-later). See the
[`LICENSE`](https://github.com/Abrinca/sylphd/blob/main/LICENSE) file in the repository.

We are open to negotiating more permissive terms: <https://abrinca.com/contact>.

The metadata database that `sylphd setup` builds is derived from GTDB data, and GTDB has its own
terms; see <https://gtdb.ecogenomic.org/>. sylph and pysylph, on which sylphd depends, are MIT
licensed.

## Third-party code

The web page vendors two small libraries, unmodified, in `web/vendor/` (with their licenses and
exact versions in the [README there](https://github.com/Abrinca/sylphd/tree/main/web/vendor)):

- [marked](https://github.com/markedjs/marked) 18.1.2, MIT License, Copyright (c) 2018+ MarkedJS and
  2011-2018 Christopher Jeffrey. Renders the Markdown text an operator publishes about a server.
- [DOMPurify](https://github.com/cure53/DOMPurify) 3.4.17, by Cure53 and contributors, used under
  the Apache License 2.0 (it is dual-licensed with the MPL 2.0). Sanitizes that rendered text.

The *use* of the public web service is covered separately by its [terms](terms.md).
