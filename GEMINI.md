# MTGA Coach developer guidance

Use the [development workflow](DEV.md) for environment setup, the [repository guidance](AGENTS.md) for architecture and local conventions, and the [feature documentation index](docs/README.md) for the subsystem you are changing.

The desktop app runs coaching in a separate Python process. Manual coaching reads Arena’s log. Autoplay uses the platform bridge. Hosted inference selects `current` through the public Coach gateway. Customer setup belongs in [INSTALL.md](INSTALL.md) and release procedures in [RELEASE.md](RELEASE.md).
