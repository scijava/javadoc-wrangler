#!/usr/bin/env python

#
# wrangle.py - Unpack javadoc JARs into a coherent multi-project structure.
#

import logging, os, re, subprocess, sys
from pathlib import Path
from typing import Sequence
from urllib import request
from xml.etree import ElementTree as ET
from zipfile import ZipFile

# -- Constants --

scriptDir = Path(__file__).parent
baseDir = scriptDir / "target"
siteBase = baseDir / "site"
workBase = baseDir / "work"
jarDir = baseDir / "jars"

toplevel_html_docs = {
    "about.html",
    "allclasses-frame.html",
    "allclasses-index.html",
    "allclasses-noframe.html",
    "allclasses.html",
    "allpackages-index.html",
    "constant-values.html",
    "deprecated-list.html",
    "help-doc.html",
    "index-all.html",
    "index.html",
    "overview-frame.html",
    "overview-summary.html",
    "overview-tree.html",
    "package-frame.html",
    "package-summary.html",
    "package-tree.html",
    "package-use.html",
    "serialized-form.html"
}

# -- Logging --

log = logging.getLogger(__name__)

def die(message, code=1):
    log.error(message)
    sys.exit(code)

# -- Classes --

class GAV:
    def __init__(self, g, a, v):
        self.g = g
        self.a = a
        self.v = v

    def __str__(self):
        return f"{self.g}:{self.a}:{self.v}"

    @property
    def valid(self):
        return bool(self.g and self.a and self.v)

class XML:

    def __init__(self, source):
        if isinstance(source, str) and source.startswith('<'):
            # Parse XML from string.
            # https://stackoverflow.com/a/18281386/1207769
            self.tree = ET.ElementTree(ET.fromstring(source))
        else:
            # Parse XML from file.
            self.tree = ET.parse(source)
        XML._strip_ns(self.tree.getroot())

    def elements(self, path):
        return self.tree.findall(path)

    def value(self, path):
        el = self.elements(path)
        assert len(el) <= 1
        return None if len(el) == 0 else el[0].text

    @staticmethod
    def _strip_ns(el):
        """
        Remove namespace prefixes from elements and attributes.
        Credit: https://stackoverflow.com/a/32552776/1207769
        """
        if el.tag.startswith("{"):
            el.tag = el.tag[el.tag.find("}")+1:]
        for k in list(el.attrib.keys()):
            if k.startswith("{"):
                k2 = k[k.find("}")+1:]
                el.attrib[k2] = el.attrib[k]
                del el.attrib[k]
        for child in el:
            XML._strip_ns(child)

# -- Functions --

def mkdirs(path):
    path.mkdir(parents=True, exist_ok=True)

def readfile(path):
    try:
        with open(path) as f:
            return f.readlines()
    except Exception as e:
        log.warning(f"Failed to read file {path}")
        log.debug(e)

def writefile(path, lines=None, append=False):
    with open(path, "a" if append else "w") as f:
        if lines is not None:
            f.writelines(lines)

def execute(cmd: Sequence[str], die_on_error=True):
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        error_message = f"Command {cmd[0]} failed with exit code {result.returncode}"
        if die_on_error:
            die(error_message)
        else:
            raise RuntimeError(error_message)
    return result.stdout.decode().splitlines(keepends=True)

def mvn(goal: Sequence[str], pom=None, die_on_error=True, **kwargs):
    cmd = ["mvn", "-B", "-s", "settings.xml"]
    if pom is not None:
        cmd.extend(["-f", str(pom)])
    cmd.append(goal)
    for k, v in kwargs.items():
        cmd.append(f"-D{k}={v}")
    return execute(cmd, die_on_error=die_on_error)

def squash(path: Path):
    if not Path(path).exists():
        die(f"No such file: {path}")
    try:
        writefile(path, sorted(set(readfile(path))))
    except Exception as e:
        log.error(f"Exception squashing {path}")
        log.debug(e)

def unpack_javadoc(c: GAV, jarFile: Path, javadocDir: Path):
    if javadocDir.exists():
        log.info(f"Skipping already unpacked {c}")
        return

    log.info(f"Unpacking javadoc JAR for {c}")
    mkdirs(javadocDir)
    with ZipFile(jarFile) as z:
        z.extractall(javadocDir)

    # Grab this component's associated POM.
    log.info(f"Copying POM for {c}")
    mvn("dependency:copy", artifact=f"{c}:pom", outputDirectory=javadocDir)

    # Replace old javadoc.scijava.org links with new ones:
    # javadoc.scijava.org/*/ -> javadoc.scijava.org/{parent.g}/{parent.a}/{parent.v}/
    pom = javadocDir / f"{c.a}-{c.v}.pom"
    xml = XML(pom)
    parent = GAV(xml.value("parent/groupId"),
                 xml.value("parent/artifactId"),
                 xml.value("parent/version"))

    if not parent.valid:
        log.warning(f"Could not glean parent POM for artifact {c}; skipping link replacement")
        return

    log.info(f"Replacing links for {c} javadoc")
    oldLink = "https?://javadoc.(scijava.org|imagej.net)/[^/]*/"
    newLink = f"/{parent.g}/{parent.a}/{parent.v}/"
    for f in javadocDir.rglob("*"):
        if f.suffix != '.html' or not f.is_file():
            continue
        try:
            writefile(f, [re.sub(oldLink, newLink, line) for line in readfile(f)])
        except Exception as e:
            log.error(f"Exception replacing links for {f}")
            log.debug(e)


def process_component(c: GAV, bom: GAV, bomDir: Path):
    # Obtain the javadoc classifier JAR.
    jarFile = jarDir / f"{c.a}-{c.v}-javadoc.jar"
    if not jarFile.exists():
        missingFile = jarFile.with_suffix(".missing")
        if missingFile.exists():
            log.warning(f"No javadoc archive for {c} (cached)")
            return

        log.info(f"Downloading/copying javadoc archive: {jarFile.name}")
        mkdirs(jarDir)
        try:
            mvn("dependency:copy", die_on_error=False,
                artifact=f"{c}:jar:javadoc",
                outputDirectory=jarDir)
        except RuntimeError as e:
            log.warning(f"No javadoc archive for {c}")
            log.debug(e)
            writefile(missingFile)
            return

    # Unpack javadoc JAR into dedicated folder.
    javadocDir = siteBase / c.g / c.a / c.v
    unpack_javadoc(c, jarFile, javadocDir)

    # Append this artifact's indices to the BOM's aggregated indices.
    log.info(f"Appending {c} package lists to {bom}")
    for packageIndexName in ("package-list", "element-list"):
        componentPackageIndex = javadocDir / packageIndexName
        if componentPackageIndex.exists():
            bomPackageIndex = bomDir / packageIndexName
            try:
                writefile(bomPackageIndex, readfile(componentPackageIndex), append=True)
            except Exception as e:
                log.error(f"Exception appending {packageIndexName} for {c}")
                log.debug(e)

    # Append artifact's class links to BOM folder's .htaccess redirects.
    log.info(f"Appending {c} htaccess rules to {bom}")
    for f in javadocDir.rglob("*"):
        # Process only Java class and package HTML documents, not toplevel ones.
        if f.suffix != '.html' or f.name in toplevel_html_docs or not f.is_file():
            continue
        relativePath = str(f)[len(str(javadocDir)):] # /
        bomPath = f"/{bom.g}/{bom.a}/{bom.v}/{relativePath}"
        componentPath = f"/{c.g}/{c.a}/{c.v}/{relativePath}"
        redirect = f"RedirectMatch permanent \"^{bomPath}$\" {componentPath}\n"
        writefile(bomDir / ".htaccess", [redirect], append=True)

def process_bom(bom: GAV):
    workDir = workBase / bom.g / bom.a / bom.v

    completeMarker = workDir / "complete"
    if completeMarker.exists():
        # Already processed this version of the BOM.
        log.info(f"Skipping already processed BOM {bom}")
        return

    mkdirs(workDir)

    log.info(f"Processing BOM {bom}")

    bomDir = siteBase / bom.g / bom.a / bom.v
    mkdirs(bomDir)

    # Download the BOM file.
    bomFile = workDir / f"{bom.a}-{bom.v}.pom"
    if not bomFile.exists():
        log.info(f"Downloading BOM {bom}")
        mvn("dependency:copy",
            artifact=f"{bom}:pom",
            outputDirectory=workDir)

    # Interpolate the BOM and extract the list of managed dependencies as XML.
    bomComponentsFile = workDir / "components.xml"
    if not bomComponentsFile.exists():
        output = mvn("help:effective-pom", bomFile)
        start = end = None
        for i, line in enumerate(output):
            if start is not None and end is not None: break
            if line.startswith('  <dependencyManagement>'): start = i
            elif line.startswith('  </dependencyManagement>'): end = i
        if start is None or end is None:
            die(f"Could not interpolate the BOM -- mvn output follows:\n{''.join(output)}")
        writefile(bomComponentsFile, output[start:end+1])
    bomComponents = XML(bomComponentsFile)

    for dep in bomComponents.elements('dependencies/dependency'):
        c = GAV(dep.find('groupId').text,
                dep.find('artifactId').text,
                dep.find('version').text)
        if c.valid:
            process_component(c, bom, bomDir)
        else:
            log.warning(f"Invalid component: {c}")

    # Sort package-list, element-list, and htaccess files, squashing duplicates.
    squash(bomDir / "package-list")
    squash(bomDir / "element-list")
    squash(bomDir / ".htaccess")

    log.info(f"Done processing BOM {bom}")
    writefile(completeMarker)

    # TODO: Check that javadoc tool actually works pointed at a BOM prefix.
    # TODO: Close https://github.com/scijava/pom-scijava/issues/130 when done.

    # QUESTIONS:
    # - What should the root of javadoc.scijava.org serve now?
    #   An index of available components? E.g. net.imagej:imagej, sc.fiji:fiji
    # - Should we aggregate the JSON indices (*-search-index.zip for all components)?
    #   What else would we need to do to make the search work for a BOM's javadoc index?
    # - Are there other documents we should aggregate like the various toplevel HTML files?

# 3. Loop over the <dependency> elements:
#    - Obtain the -javadoc JAR for that dependency (efficiently!).
#      - If already copied/linked, do nothing.
#      - Copy/link from local file system if available.
#        Right now, javadoc.scijava.org resides on devonrex, but if
#        moved to balinese, it could fetch existing cached artifacts
#        from the local file system, which would be even faster.
#      - `mvn dependency:get` if not available locally.
#      - Fail gracefully and continue if it doesn't exist.
#    - Extract the JAR to its special folder.
#      From here on out, the logic in wrangle.sh should be correct.
#      Just need to translate it into Python.

# Authoritative list of published pom-scijava versions:
# https://repo1.maven.org/maven2/org/scijava/pom-scijava/maven-metadata.xml

# Ultimately, the goal is to wrangle every published version of
# pom-scijava, so the javadoc is as complete as possible.

# -- Main --

def main(args=None):
    logging.basicConfig(level=logging.DEBUG, format="[%(levelname)s] %(message)s")

    if args is None:
        args = []
    if len(args) == 0:
        # Use the latest release of pom-scijava if no args given.
        try:
            url = "https://repo1.maven.org/maven2/org/scijava/pom-scijava/maven-metadata.xml"
            metadata = XML(request.urlopen(url).read().decode())
            version = metadata.value('versioning/release')
        except Exception as e:
            log.debug(e)
            version = None
        if not version:
            die("Cannot glean latest version of org.scijava:pom-scijava.")
        args.append(version)

    for arg in args:
        if ":" in arg:
            gav = ":".split(arg)
            bom = GAV(*gav)
        else:
            bom = GAV("org.scijava", "pom-scijava", arg)
        process_bom(bom)

if __name__ == '__main__':
    main(sys.argv[1:])
