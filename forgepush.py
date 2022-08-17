# forgepush 1.1.1
# Packages WoW addon releases.
#
# (C) 2022 tmg <tmg@clubtammy.info>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the "Software"), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify,
# merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to the following
# conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or
# substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
# PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT
# OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.

import argparse, yaml, os, shutil, re, json, time, zipfile, requests, subprocess
from pathlib import Path

EXIT_CODE_ALREADY_PUBLISHED = -10
EXIT_CODE_GITTAG            = -11
EXIT_CODE_UNCLEAN_WD_CANCEL = -12
EXIT_CODE_NO_API_TOKEN      = -21
EXIT_CODE_FAILED_UPLOAD     = -50
EXIT_CODE_NETWORK_ERROR     = -51

CACHEFILE              = ".forgepush.cache"
DEFAULT_PACKAGE_FOLDER = ".forgepush"

#----------------------------------------------------------------------------------------
command_args = argparse.ArgumentParser( description="Push a package to CurseForge." )
command_args.add_argument( '--curse_apitoken', '-a',
   help='CurseForge API token to use. Can also set CURSEFORGE_API_TOKEN environment variable.' )
command_args.add_argument( '--github_token',
   help='GitHub API token to use.' )
command_args.add_argument( '--publish_curseforge', action='store_true',
   help='Publish the package to CurseForge.' )
command_args.add_argument( '--yesokay', '-y', action='store_true',
   help='Skip the confirmation check for publishing.' )
command_args.add_argument( '--toc',
   help='Set what TOC version to use.' )
command_args.add_argument( '--addonversion', default="dev",
   help="What addon version to write in the files.")
command_args.add_argument( '--create_github_release', action='store_true',
   help="If set, creates a github release.")

command_args = command_args.parse_args()

forgetoken = command_args.curse_apitoken
if not forgetoken:
   forgetoken = os.environ.get( "CURSEFORGE_API_TOKEN" )
if not forgetoken:
   print( "No API token to use. Set it via --curse_apitoken or env var CURSEFORGE_API_TOKEN." )
   exit( EXIT_CODE_NO_API_TOKEN )

with open( "package.yaml", "r", encoding="utf-8" ) as file:
   config = yaml.safe_load( file )

config['version'] = command_args.addonversion

# Predefined variables:
if not config.get( "variables", None ): config["variables"] = {}
config["variables"]["addon_version"] = config["version"]

# Create and enter package folder.
package_folder = config.get( "package_folder", DEFAULT_PACKAGE_FOLDER )
project_root = Path( os.getcwd() )

os.makedirs( package_folder, exist_ok=True )
os.chdir( package_folder )
if not os.path.exists( "warning.txt" ):
   with open( "warning.txt", "w" ) as f:
      f.write( "[Warning] Do not put anything in this folder. It will be erased when you run forgepush." )

cache = {}

#----------------------------------------------------------------------------------------
def load_cache():
   global cache
   if os.path.exists( CACHEFILE ):
      with open( CACHEFILE, "r", encoding="utf-8" ) as f:
         cache = json.load( f )

load_cache()

#----------------------------------------------------------------------------------------
def save_cache():
   with open( CACHEFILE, "w", encoding="utf-8" ) as f:
      json.dump( cache, f, indent = 3 )

class HttpError(ValueError):
    def __init__(self, code, message):
        self.code    = code
        self.message = message
        super().__init__(self.message)

#----------------------------------------------------------------------------------------
# Makes a request to the CurseForge API.
def curse_request( endpoint, method = "GET", query = None, body = None, extra_headers = {} ):
   headers = {}
   headers['X-Api-Token'] = forgetoken
   
   for header, value in extra_headers.items():
      headers[header] = value
   
   response = requests.request( method, "https://wow.curseforge.com" + endpoint,
                                params=query, headers=headers )
   if response.status_code != 200:
      #print( "HTTP error.", response.status_code, response.reason )
      raise HttpError( response.status_code, response.reason ) #"Failed to make request to Curse endpoint." )
   return response.text

def fetch_toc_versions():
   age_minutes = int(time.time() - cache.get( "toc_time", 0 )) // 60
   if age_minutes < 60*2:
      # Only load every 2 hours.
      return

   retail = requests.get(
      f"https://wow.gamepedia.com/api.php?action=expandtemplates&text=%7B%7BAPI_LatestInterface%7D%7D&prop=wikitext&format=json"
   )

   if( retail.status_code != 200 ):
      print( "Couldn't update TOC version from Wowpedia." )
      return
   cache["toc_retail"] = json.loads(retail.text)["expandtemplates"]["wikitext"]
   
   classic = requests.get(
      f"https://wow.gamepedia.com/api.php?action=expandtemplates&text=%7B%7BAPI_LatestInterface%7Cclassic%7D%7D&prop=wikitext&format=json"
   )

   if( classic.status_code != 200 ):
      print( "Couldn't update TOC version from Wowpedia." )
      return

   cache["toc_classic"] = json.loads(classic.text)["expandtemplates"]["wikitext"]
   cache["toc_time"] = time.time()

fetch_toc_versions()

config["variables"]["toc_version_retail"] = command_args.toc or cache["toc_retail"]
config["variables"]["toc_version_classic"] = command_args.toc or cache["toc_classic"]

#----------------------------------------------------------------------------------------
def replace_variable( term ):
   if not term in config["variables"]:
      raise ValueError( f"Unknown variable found: {term}." )
   else:
      return str(config["variables"][term])

#----------------------------------------------------------------------------------------
def include_file(fromfile, file):
   path = os.path.join(os.path.dirname(fromfile), file)
   with open(path, "r", encoding="utf-8") as f:
      content = f.read()

   content, changes = postprocess_content(path, content)
   return content

#----------------------------------------------------------------------------------------
def replace_command(path, input):
   cmd = re.match(r"^include\s*(.*)\s*$", input)
   if cmd:
      return include_file(path, cmd.group(1))

   raise ValueError( f"Unknown command: {input}" )

#----------------------------------------------------------------------------------------
def postprocess_content(path, input):
   try:
      changes = False
      r = input
      r = re.sub( "@@(.+?)@@",
                  lambda match : replace_variable( match.group(1) ), r )
      # We don't want to match newlines in padding.
      r = re.sub( r"^[ \t]*--@(.+)@[ \t]*$",
                  lambda match : replace_command(path, match.group(1) ),
                  r,
                  flags=re.MULTILINE )
      
      if input != r:
         changes = True

   except ValueError as e:
      print( "Error processing {path}.", e )
   return r, changes

#----------------------------------------------------------------------------------------
def postprocess(path):
   # Read file contents.

   if path.suffix not in [".txt", ".lua", ".toc"]:
      return

   contents = ""
   
   # We could add some exception handling here, maybe... but ideally, transient errors
   #  should be handled by a library.
   with open( path, "r", encoding="utf-8" ) as file:
      contents = file.read()

   contents, changes = postprocess_content(path, contents)

   if changes:
      with open( path, "w", encoding="utf-8" ) as file:
         file.write( contents )

#----------------------------------------------------------------------------------------
def process_files( path ):
   for dirpath, subfolders, files in os.walk( path ):
      
      for f in files:
         postprocess( Path(dirpath) / f )

#----------------------------------------------------------------------------------------
def zip_package( output_path ):
   skip = os.path.join( ".", output_path )
   with zipfile.ZipFile( output_path, 'w', zipfile.ZIP_DEFLATED) as ziph:
      for root, dirs, files in os.walk("."):
         if root == ".": continue # Don't add any files in the root folder.
         for file in files:
            filepath = os.path.join( root, file )
            ziph.write( filepath )

#----------------------------------------------------------------------------------------
def clean_package():
   for root, dirs, files in os.walk("."):
      for dir in dirs:
         shutil.rmtree( dir )
      break

save_cache()

# cleanup.
print( "Cleanup..." )
clean_package()

# Copy exports.
print( "Copying exports." )
for dest, source in config["export"].items():
   shutil.copytree( project_root / source, dest )

print( "Post-processing files." )
# Perform postprocessing.
process_files( Path(".") )

if "run" in config:
   print( "Running commands." )
   # Run post-process commands.
   for command in config.get( "run", {} ):
      print( f" - {command}" )
      os.system( command )

def load_file( path ):
   with open( path, "rb" ) as f:
      return f.read()

def get_yesno( prompt ):
   while True:
      r = input( prompt )
      if r == "y" or r == "Y": return True
      if r == "n" or r == "N": return False

def check_published( version ):
   cache["curse-published"] = cache.get( "curse-published", {} )
   if cache["curse-published"].get( version, False ):
      return True
   return False

def write_published( version ):
   cache["curse-published"] = cache.get( "curse-published", {} )
   cache["curse-published"][version] = True

#-----------------------------------------------------------------------------------------
def publish_to_curseforge():
   print( "Publishing to CurseForge." )
   if check_published( config["version"] ):
      print( "- This version is already marked as published." )
      print( f"- To republish it, delete {CACHEFILE}" )
      return
   gitstatus = subprocess.check_output( ["git", "status", "-s"] ).strip()
   if gitstatus != b"":
      print( " - Working directory is not clean. Not continuing." )
      return False

   zip_path = f"{config['name']}-{config['version']}.zip"
   print( f" - Zipping: {zip_path}" )
   zip_package( zip_path )

   if not command_args.yesokay:
      print( f" - You can view the package under {package_folder}/ before publishing." )
      if config.get( "classic", False ):
         print( f" - Current UI version is {cache['toc_classic']}. If incorrect, cancel this, correct Wowpedia, and delete {CACHEFILE}." )
      else:
         print( f" - Current UI version is {cache['toc_retail']}. If incorrect, cancel this, correct Wowpedia, and delete {CACHEFILE}." )
      input( " - Press any key to continue or Ctrl+C to cancel." )

   print( " - Fetching game version data from CurseForge..." )
   version_data = json.loads(curse_request( f"/api/game/versions" ))

   # 517 is wow retail, 67408 is wow classic
   latest_classic = [x for x in version_data if x["gameVersionTypeID"] == 67408]
   latest_classic.sort( reverse=True, key=lambda x : x["id"] )
   latest_classic = latest_classic[0]
   latest_retail = [x for x in version_data if x["gameVersionTypeID"] == 517]
   latest_retail.sort( reverse=True, key=lambda x : x["id"] )
   latest_retail = latest_retail[0]
   print( f"   - Retail  : {latest_retail['id']} - {latest_retail['name']}" )
   print( f"   - Classic : {latest_classic['id']} - {latest_classic['name']}" )

   gameVersions = []

   if config.get( "classic", False ):
      gameVersions.append( latest_classic["id"] )
      print( " - This project is being published to Classic WoW." )

   else:
      gameVersions.append( latest_retail["id"] )
      print( " - This project is being published to Retail WoW." )

   releaseType = "release"
   if re.search( "alpha", config['version'], re.IGNORECASE ):
      releaseType = "alpha"
   elif re.search( "beta", config['version'], re.IGNORECASE ):
      releaseType = "beta"

   print( " - Uploading..." )

   gitlog = subprocess.check_output( ["git", "log", "-10"] ).decode('utf-8')

   metadata = json.dumps({
      "changelog"    : gitlog,
      "changelogType" : "text",
      "displayName"  : config['version'],
      "gameVersions" : gameVersions,
      "releaseType"  : releaseType
   })

   response = requests.post(
      f"https://wow.curseforge.com/api/projects/{config['curse-project-id']}/upload-file",
      headers = { "X-Api-Token" : forgetoken },
      data    = { "metadata" : metadata },
      files   = { "file": (zip_path, open( zip_path, "rb" )) }
   )

   if( response.status_code != 200 ):
      print( " - Bad status from upload." )
      return False

   print( " - Upload complete.", response.text )
   write_published( config["version"] )
   return True

#-----------------------------------------------------------------------------------------
def publish_to_github():
   tagname = config["version"]

   parse_github = re.match(r"^https://github.com/([^/]+)/([^/]+)$")
   github_owner = parse_github.group(1)
   github_repo  = parse_github.group(2)
   
   release_name = config["name"] + " " + config["version"]

   response = requests.post(
      f"https://api.github.com/repos/{github_owner}/{github_repo}/releases",
      headers = {
         "Accept": "application/vnd.github+json",
         "Authorization" : "token " + command_args.github_token
      },
      json = {
         "tag_name": config["version"],
         "name": release_name
      },
   )

if command_args.publish_curseforge:
   publish_to_curseforge()

if command_args.create_github_release:
   publish_to_github()

save_cache()
