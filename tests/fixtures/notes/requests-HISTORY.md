Release History
===============

dev
---

- \[Short description of non-trivial change.\]


2.34.2 (2026-05-14)
-------------------
- Moved `headers` input type back to `Mapping` to avoid invariance issues
  with `MutableMapping` and inferred dict types. Users calling
  `Request.headers.update()` may need to narrow typing in their code. (#7441)


2.34.1 (2026-05-13)
-------------------

**Bugfixes**
- Widened `json` input type from `dict` and `list` to `Mapping`
  and `Sequence`. (#7436)
- Changed `headers` input type to MutableMapping and removed `None` from
  `Request.headers` typing to improve handling for users. (#7431)
- `Response.reason` moved from `str | None` to `str` to improve handling
  for users. (#7437)
- Fixed a bug where some bodies with custom `__getattr__` implementations
  weren't being properly detected as Iterables. (#7433)


2.34.0 (2026-05-11)
-------------------

**Announcements**
- Requests 2.34.0 introduces inline types, replacing those provided by
  typeshed. Public API types should be fully compatible with mypy, pyright,
  and ty. We believe types are comprehensive but if you find issues, please
  report them to the pinned tracking issue.

  Special thanks to @bastimeyer, @cthoyt, @edgarrmondragon, and @srittau for
  helping review and test the types ahead of the release. (#7272)

**Improvements**
- Digest Auth hashing algorithms have added `usedforsecurity=False` to clarify
  security considerations. (#7310)
- Requests added support for Python 3.15 based on beta1. Downstream projects
  should be able to start testing prior to its release in October. (#7422)
- Requests added support for Python 3.14t. (#7419)

**Bugfixes**
- ``Response.history`` no longer contains a reference to itself, preventing
  accidental looping when traversing the history list. (#7328)
- Requests no longer performs greedy matching on no_proxy domains. The
  proxy_bypass implementation has been updated with CPython's fix from
  bpo-39057. (#7427)
- Requests no longer incorrectly strips duplicate leading slashes in
  URI paths. This should address user issues with specific presigned
  URLs. Note the full fix requires urllib3 2.7.0+. (#7315)


2.33.1 (2026-03-30)
-------------------

**Bugfixes**
- Fixed test cleanup for CVE-2026-25645 to avoid leaving unnecessary
  files in the tmp directory. (#7305)
- Fixed Content-Type header parsing for malformed values. (#7309)
- Improved error consistency for malformed header values. (#7308)

2.33.0 (2026-03-25)
-------------------

**Announcements**
- 📣 Requests is adding inline types. If you have a typed code base that
uses Requests, please take a look at #7271. Give it a try, and report
any gaps or feedback you may have in the issue. 📣

**Security**
- CVE-2026-25645 ``requests.utils.extract_zipped_paths`` now extracts
  contents to a non-deterministic location to prevent malicious file
  replacement. This does not affect default usage of Requests, only
  applications calling the utility function directly.

**Improvements**
- Migrated to a PEP 517 build system using setuptools. (#7012)

**Bugfixes**
- Fixed an issue where an empty netrc entry could cause
  malformed authentication to be applied to Requests on
  Python 3.11+. (#7205)

**Deprecations**
- Dropped support for Python 3.9 following its end of support. (#7196)

**Documentation**
- Various typo fixes and doc improvements.


2.32.5 (2025-08-18)
-------------------

**Bugfixes**

- The SSLContext caching feature originally introduced in 2.32.0 has created
  a new class of issues in Requests that have had negative impact across a number
  of use cases. The Requests team has decided to revert this feature as long term
  maintenance of it is proving to be unsustainable in its current iteration.

**Deprecations**
- Added support for Python 3.14.
- Dropped support for Python 3.8 following its end of support.

2.32.4 (2025-06-10)
-------------------

**Security**
- CVE-2024-47081 Fixed an issue where a maliciously crafted URL and trusted
  environment will retrieve credentials for the wrong hostname/machine from a
  netrc file.

**Improvements**
- Numerous documentation improvements

**Deprecations**
- Added support for pypy 3.11 for Linux and macOS.
- Dropped support for pypy 3.9 following its end of support.


2.32.3 (2024-05-29)
-------------------

**Bugfixes**
- Fixed bug breaking the ability to specify custom SSLContexts in sub-classes of
  HTTPAdapter. (#6716)
- Fixed issue where Requests started failing to run on Python versions compiled
  without the `ssl` module. (#6724)

2.32.2 (2024-05-21)
-------------------

**Deprecations**
- To provide a more stable migration for custom HTTPAdapters impacted
  by the CVE changes in 2.32.0, we've renamed `_get_connection` to
  a new public API, `get_connection_with_tls_context`. Existing custom
  HTTPAdapters will need to migrate their code to use this new API.
  `get_connection` is considered deprecated in all versions of Requests>=2.32.0.

  A minimal (2-line) example has been provided in the linked PR to ease
  migration, but we strongly urge users to evaluate if their custom adapter
  is subject to the same issue described in CVE-2024-35195. (#6710)
