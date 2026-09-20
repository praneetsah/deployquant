# Security

DQengine can place real orders, and it runs untrusted strategy code in a
sandbox. Security reports are handled privately.

## Reporting a problem

Please do not open a public issue. Use GitHub's private vulnerability reporting:
on this repository, go to Security and then "Report a vulnerability". Include
what you found, how to reproduce it, and the version or commit.

You will get a reply within 5 business days. When a fix is ready, we will agree
with you when to make it public, and credit you unless you ask us not to.

## What counts

- A way out of the strategy sandbox (`dqengine.sandbox`), or a weakness in it.
- Anything that could send an order the strategy did not ask for. That includes
  a duplicated order, a wrong size, a wrong side or a wrong symbol. It also
  includes anything that gets around the order journal, the duplicate-order
  check or a safety check.
- How the broker adapters and plugins handle credentials.
- Dependency or packaging problems that affect an installed copy.

## What does not count

- A strategy that loses money. See [DISCLAIMER.md](DISCLAIMER.md).
- A vulnerability in a broker's or data vendor's own API. Report that to them.
  Tell us as well if one of our adapters makes it worse.

## Supported versions

Security fixes go into the latest release. There are no maintained older
branches before version 1.0.
