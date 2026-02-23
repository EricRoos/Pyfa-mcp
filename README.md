# pyfa

[![Build Status](https://ci.appveyor.com/api/projects/status/github/pyfa-org/pyfa?branch=master&svg=true)]([https://travis-ci.org/pyfa-org/Pyfa](https://ci.appveyor.com/project/pyfa-org/pyfa))

![pyfa](https://user-images.githubusercontent.com/275209/66119992-864be080-e5e2-11e9-994a-3a4368c9fad7.png)

## What is it?

Pyfa, short for **py**thon **f**itting **a**ssistant, allows you to create, experiment with, and save ship fittings without being in game. Open source and written in Python, it is available on any platform where Python 3 and wxWidgets are available, including Windows, macOS, and Linux.

## Latest Version and Changelogs
The latest version along with release notes can always be found on the project's [releases](https://github.com/pyfa-org/Pyfa/releases) page. Pyfa will notify you if you are running an outdated version.

## Installation
Windows, macOS, and Linux users are supplied self-contained builds of pyfa on the [latest releases](https://github.com/pyfa-org/Pyfa/releases/latest) page.

### Third Party Packages
Please note that these packages are maintained by third-parties and are not evaluated by the pyfa developers.

#### macOS
Apart from the official release, there is also a [Homebrew](https://formulae.brew.sh/cask/pyfa) option for installing pyfa on macOS. Simply fire up in terminal:
```
$ brew install --cask pyfa
```

#### Linux Distro-specific Packages
The following is a list of pyfa packages available for certain distributions. 

* Arch: https://aur.archlinux.org/packages/pyfa/
* Gentoo: https://github.com/ZeroPointEnergy/gentoo-pyfa-overlay

## Contribution
If you wish to help with development or you need to run pyfa through a Python interpreter, check out [the instructions](https://github.com/pyfa-org/Pyfa/blob/master/CONTRIBUTING.md).

## Headless MCP Mode (experimental)
pyfa can run as a headless MCP server over stdio for AI-agent workflows.

Start it with:

```bash
python pyfa.py --mcp
```

Current MCP tools:

- `fit.import_text` - import EFT/pyfa copy-paste fit text into an ephemeral in-memory session
- `hull.get_slot_summary` - return authoritative slot/hardpoint counts for a hull directly from pyfa data
- `fit.get_stats` - return structured fit stats (DPS, DPS at range, EHP, tank, cap, mobility, fitting resources)
- `market.get_prices` - expose pyfa cached market price data (price/status/age/validity) for items or an entire fit
- `fit.set_profiles` - set target profile and/or damage pattern on a fit using pyfa builtins or user-defined profiles
- `fit.validate_and_explain` - run pyfa validity checks and return actionable reasons if a fit is invalid
- `fit.optimize_pareto` - tune module states and return Pareto-optimal variants across offense, defense, and mobility metrics
- `fit.list_modules` - list all module slots (with stable slot indexes) for agent-driven fit edits
- `fit.compare_slot_candidates` - compare replacement modules for a slot and return score deltas/snapshots
- `fit.apply_slot_candidate` - apply a chosen replacement to a slot and return updated fit stats
- `fit.optimize_iterative` - iterate compare/swap loops across slots until objective improvement converges
- `fit.export_eft` - export any session fit back to EFT text

Notes:

- This mode is headless (no GUI windows) and does not persist fits unless your client exports and saves them.
- Optimization scope is still constrained to in-fit/manual-like workflows and discovered candidate modules; it does not attempt unrestricted market-wide ship auto-fitting.
- Agent quality gate: before returning fitting advice to users, agents should run pyfa validation/optimization tools (`fit.get_stats`, `fit.optimize_iterative` and/or `fit.optimize_pareto`) on the candidate fit and report those verified results, not unverified estimates.

## Bug Reporting
The preferred method of reporting bugs is through the project's [GitHub Issues interface](https://github.com/pyfa-org/Pyfa/issues). Alternatively, posting a report in the [pyfa thread](https://forums.eveonline.com/t/27156) on the official EVE Online forums is acceptable. Guidelines for bug reporting can be found on [this wiki page](https://github.com/pyfa-org/Pyfa/wiki/Bug-Reporting). 

## License
Pyfa is licensed under the GNU GPL v3.0, see LICENSE

## Resources
* [Development repository](https://github.com/pyfa-org/Pyfa)
* [EVE forum thread](https://forums.eveonline.com/t/27156)
* [EVE University guide using pyfa](https://wiki.eveuniversity.org/PYFA)
* [EVE Online website](http://www.eveonline.com/)

## Contacts:
* Kadesh / DarkPhoenix
    * GitHub: @DarkFenX
    * EVE: Kadesh Priestess
    * Email: phoenix@mail.ru
* Sable Blitzmann
    * GitHub: @blitzmann
    * [TweetFleet Slack](https://www.fuzzwork.co.uk/tweetfleet-slack-invites/): @blitzmann
    * [Gitter chat](https://gitter.im/pyfa-org/Pyfa): @blitzmann
    * Email: sable.blitzmann@gmail.com

## CCP Copyright Notice
EVE Online, the EVE logo, EVE and all associated logos and designs are the intellectual property of CCP hf. All artwork, screenshots, characters, vehicles, storylines, world facts or other recognizable features of the intellectual property relating to these trademarks are likewise the intellectual property of CCP hf. EVE Online and the EVE logo are the registered trademarks of CCP hf. All rights are reserved worldwide. All other trademarks are the property of their respective owners. CCP hf. has granted permission to pyfa to use EVE Online and all associated logos and designs for promotional and information purposes on its website but does not endorse, and is not in any way affiliated with, pyfa. CCP is in no way responsible for the content on or functioning of this program, nor can it be liable for any damage arising from the use of this program.
