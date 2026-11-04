# Third-party origins

QQArchiveNext is distributed under GPL-3.0. The byte-preserving shared content pool and local WebView2 architecture reuse CaptureNext (https://github.com/lin522026298/CaptureNext), also distributed under GPL-3.0. No WeChat data or credentials are included.

The NTQQ Protobuf parser and generated protocol definitions are vendored from nt_msg_db_util; vendor/nt_msg_db_util/SOURCE.json records the exact component file hashes. Its original GPL-3.0 license and complete used source files are retained.

The independent read-only native reader is built from SQLite public-domain source. Build-QqNativeReader.py pins the upstream source and compiler hashes and checks the small pending-byte relocation patch. The archive includes original SQLite source, patched C, flags and runtime metadata. It does not patch QQ or the system SQLite library.

Microsoft WebView2 SDK DLLs retain Microsoft's original license; the separate Windows WebView2 Runtime is not bundled. Python and third-party runtime licenses are included in the program distribution.
