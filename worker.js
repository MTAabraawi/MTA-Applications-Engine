import postgres from "postgres";

const DEFAULT_ORIGINS = ["https://www.mtahqq.dpdns.org", "https://mtahqq.dpdns.org"];
const CURATED_WEB = [
  {id:"mta-website",name:"MTA Apps Center",developer:"MTA Applications Team",summary:"Official MTA application discovery portal",description:"A web-based application store for discovering multi-platform software catalogs.",category:"portal",app_url:"https://www.mtahqq.dpdns.org",icon_url:"https://www.mtahqq.dpdns.org/icon.png",homepage:"https://www.mtahqq.dpdns.org"},
  {id:"firefox",name:"Mozilla Firefox",developer:"Mozilla Foundation",summary:"Free and open-source web browser",category:"browser",app_url:"https://www.mozilla.org/firefox",homepage:"https://www.mozilla.org"},
  {id:"libreoffice",name:"LibreOffice",developer:"The Document Foundation",summary:"Free office suite",category:"office",app_url:"https://www.libreoffice.org",homepage:"https://www.libreoffice.org"},
  {id:"gimp",name:"GIMP",developer:"GIMP Team",summary:"GNU Image Manipulation Program",category:"photo",app_url:"https://www.gimp.org",homepage:"https://www.gimp.org"},
  {id:"vivaldi",name:"Vivaldi",developer:"Vivaldi Technologies",summary:"Customizable web browser",category:"browser",app_url:"https://vivaldi.com",homepage:"https://vivaldi.com"},
  {id:"onlyoffice",name:"ONLYOFFICE",developer:"ONLYOFFICE",summary:"Office suite with collaboration",category:"office",app_url:"https://www.onlyoffice.com",homepage:"https://www.onlyoffice.com"},
  {id:"blender",name:"Blender",developer:"Blender Foundation",summary:"3D creation suite",category:"utilities",app_url:"https://www.blender.org",homepage:"https://www.blender.org"}
];
let cachedSql;
let githubCatalogueCache = new Map();

function githubStorageEnabled(env) {
  return String(env.CATALOGUE_STORAGE || "").toLowerCase() === "github";
}
async function githubJson(env, relativePath, allowMissing = false) {
  const branch = env.GITHUB_CATALOGUE_BRANCH || "main";
  const url = "https://raw.githubusercontent.com/MTAabraawi/MTA-Applications-Engine/" +
    branch + "/catalogue_data/" + relativePath.split("/").map(encodeURIComponent).join("/");
  const cached = githubCatalogueCache.get(url);
  if (cached && cached.expiresAt > Date.now()) return cached.value;
  const response = await fetch(url, {cf:{cacheTtl:300,cacheEverything:true}});
  if (allowMissing && response.status === 404) return {};
  if (!response.ok) throw new Error("GitHub catalogue file unavailable (" + response.status + "): " + relativePath);
  const value = await response.json();
  githubCatalogueCache.set(url, {value, expiresAt:Date.now()+30000});
  if (githubCatalogueCache.size > 40) githubCatalogueCache.delete(githubCatalogueCache.keys().next().value);
  return value;
}
function catalogueSearchGrams(value) {
  const grams = new Set();
  const tokens = String(value || "").toLowerCase().match(/[\p{L}\p{N}.+#-]{2,}/gu) || [];
  for (const token of tokens) {
    const width = token.length === 2 ? 2 : 3;
    for (let index = 0; index <= token.length - width; index++) grams.add(token.slice(index, index + width));
  }
  return [...grams];
}
async function catalogueTokenBucket(token) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
  return new Uint8Array(digest)[0].toString(16).padStart(2, "0");
}
async function catalogueShardNumber(key, count) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(key));
  return (new DataView(digest).getUint32(0, false) % count).toString().padStart(4, "0");
}
async function githubRecordsForKeys(env, keys, shardCount) {
  const grouped = new Map();
  for (const key of keys) {
    const shard = await catalogueShardNumber(key, shardCount);
    if (!grouped.has(shard)) grouped.set(shard, []);
    grouped.get(shard).push(key);
  }
  const records = [];
  await Promise.all([...grouped.entries()].map(async ([shard, wanted]) => {
    const payload = await githubJson(env, "shards/" + shard + ".json");
    for (const key of wanted) if (payload[key]) records.push(payload[key]);
  }));
  return records;
}
async function githubListApps(env, url) {
  const manifest = await githubJson(env, "manifest.json");
  if (manifest.schema_version !== 1 || manifest.storage !== "git-json-shards") throw new Error("Unsupported GitHub catalogue schema");
  if (manifest.search_index_format !== "trigram-v1") throw new Error("Unsupported GitHub catalogue search index format");
  const platform = (url.searchParams.get("platform") || "").toLowerCase();
  const source = (url.searchParams.get("source") || "").toLowerCase();
  const limit = Math.max(1, Math.min(Number(url.searchParams.get("limit") || 50), 200));
  const offset = Math.max(0, Number(url.searchParams.get("offset") || 0));
  const [browse, platforms, sources] = await Promise.all([
    url.searchParams.get("q") ? Promise.resolve([]) : githubJson(env, "indexes/browse.json"),
    platform ? githubJson(env, "indexes/platform.json") : Promise.resolve({}),
    source ? githubJson(env, "indexes/source.json") : Promise.resolve({})
  ]);
  const platformSet = platform ? new Set(platforms[platform] || []) : null;
  const sourceSet = source ? new Set(sources[source] || []) : null;
  const q = (url.searchParams.get("q") || "").trim();
  let keys;
  if (q) {
    const grams = catalogueSearchGrams(q);
    if (!q) keys = browse.filter(key => (!platformSet || platformSet.has(key)) && (!sourceSet || sourceSet.has(key)));
    else if (!grams.length) keys = [];
    else {
      let candidates = null;
      for (const gram of grams) {
        const bucket = await catalogueTokenBucket(gram);
        const index = await githubJson(env, "indexes/search/" + bucket + ".json", true);
        const current = new Set(index[gram] || []);
        candidates = candidates === null ? current : new Set([...candidates].filter(key => current.has(key)));
      }
      keys = [...(candidates || [])].filter(key => (!platformSet || platformSet.has(key)) && (!sourceSet || sourceSet.has(key)));
      const records = await githubRecordsForKeys(env, keys, manifest.shard_count);
      const needle = q.trim().toLowerCase();
      const filtered = records.filter(row => ["name","developer","summary","description","category"]
        .some(field => String(row[field] || "").toLowerCase().includes(needle)));
      filtered.sort((a,b) => String(a.name||"").localeCompare(String(b.name||"")) ||
        String(a.platform||"").localeCompare(String(b.platform||"")) ||
        String(a.source_key||"").localeCompare(String(b.source_key||"")) ||
        String(a.external_id||"").localeCompare(String(b.external_id||"")));
      const page = filtered.slice(offset, offset + limit).map(row => ({
        id: row.source_key + ":" + row.external_id, source: row.source_key, external_id: row.external_id,
        platform: row.platform, name: row.name, developer: row.developer, summary: row.summary,
        description: row.description, category: row.category, license: row.license,
        source_url: row.source_url, app_url: row.app_url, icon_url: row.icon_url,
        metadata_json: row.metadata_json || {}, first_seen_at: row.first_seen_at, updated_at: row.updated_at
      }));
      return {rows:page,total:filtered.length};
    }
  } else {
    keys = browse.filter(key => (!platformSet || platformSet.has(key)) && (!sourceSet || sourceSet.has(key)));
  }
  const total = keys.length;
  const pageKeys = keys.slice(offset, offset + limit);
  const records = await githubRecordsForKeys(env, pageKeys, manifest.shard_count);
  const byKey = new Map(records.map(row => [row.source_key + ":" + row.external_id, row]));
  const rows = pageKeys.map(key => byKey.get(key)).filter(Boolean).map(row => ({
    id: row.source_key + ":" + row.external_id, source: row.source_key, external_id: row.external_id,
    platform: row.platform, name: row.name, developer: row.developer, summary: row.summary,
    description: row.description, category: row.category, license: row.license,
    source_url: row.source_url, app_url: row.app_url, icon_url: row.icon_url,
    metadata_json: row.metadata_json || {}, first_seen_at: row.first_seen_at, updated_at: row.updated_at
  }));
  return {rows,total};
}

function database(env) {
  if (!env.HYPERDRIVE || !env.HYPERDRIVE.connectionString) throw new Error("Hyperdrive binding is unavailable");
  if (!cachedSql) cachedSql = postgres(env.HYPERDRIVE.connectionString, {max:5,idle_timeout:20,connect_timeout:10,fetch_types:false,prepare:false});
  return cachedSql;
}
async function queryWithRetry(env, statement) {
  let lastError;
  for (let attempt=0; attempt<3; attempt++) {
    try {
      return await database(env).unsafe(statement);
    } catch (error) {
      lastError = error;
      // Hyperdrive connections can become stale between Worker requests.
      // Discard the failed client and establish a fresh connection before retrying.
      cachedSql = null;
      if (attempt < 2) await new Promise(resolve => setTimeout(resolve, 150 * (attempt + 1)));
    }
  }
  throw lastError;
}
function response(data,status,origin,extraHeaders={}) {
  return new Response(JSON.stringify(data),{status:status,headers:{"content-type":"application/json; charset=utf-8","access-control-allow-origin":origin,"access-control-allow-methods":"GET, POST, OPTIONS","access-control-allow-headers":"Content-Type, X-Admin-Token","vary":"Origin",...extraHeaders}});
}
function corsOrigin(request,env) {
  const allowed=(env.CORS_ORIGINS||DEFAULT_ORIGINS.join(",")).split(",").map(x=>x.trim()).filter(Boolean);
  const origin=request.headers.get("Origin")||"";
  if(!origin)return "*";
  if(allowed.includes(origin))return origin;
  // The catalogue GET endpoints expose public metadata only. Permit public website
  // origins so a stale CORS_ORIGINS variable cannot break the public marketplace.
  const requestedMethod=request.headers.get("Access-Control-Request-Method")||request.method;
  return requestedMethod==="GET"?"*":"null";
}
function safeHttps(value) {
  if(typeof value!=="string")return null;
  const s=value.trim();
  try { const u=new URL(s); return u.protocol==="https:"&&u.hostname&&!u.username&&!u.password?s:null; }
  catch { return null; }
}
function cleanText(value,max=10000) {
  if(typeof value!=="string")return null;
  let s=value.replace(/<(script|style)[^>]*>[\s\S]*?<\/\1>/gi," ")
    .replace(/<(br|\/p|\/li|\/h[1-6]|\/div)[^>]*>/gi,"\n").replace(/<[^>]+>/g," ");
  s=s.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi,(match,entity)=>{
    const key=entity.toLowerCase(),named={amp:"&",lt:"<",gt:">",quot:'"',apos:"'",nbsp:" ",ndash:"–",mdash:"—",hellip:"…",copy:"©",reg:"®"};
    if(key in named)return named[key];
    try { const code=key.startsWith("#x")?parseInt(key.slice(2),16):key.startsWith("#")?parseInt(key.slice(1),10):NaN; return Number.isFinite(code)&&code>0&&code<=0x10ffff?String.fromCodePoint(code):match; }
    catch { return match; }
  });
  s=s.replace(/[\u200B-\u200D\uFEFF]/g,"").replace(/\r\n?/g,"\n").replace(/[ \t]+/g," ").replace(/\n\s*\n+/g,"\n").trim();
  return s?s.slice(0,max):null;
}
function record(source,id,platform,name,sourceUrl,extra={}) {
  const cleanName=cleanText(name,500),url=safeHttps(sourceUrl);
  if(id==null||!String(id).trim()||!cleanName||!url)return null;
  return {source_key:source,external_id:String(id).trim(),platform:platform,name:cleanName,
    developer:cleanText(extra.developer),summary:cleanText(extra.summary),description:cleanText(extra.description),category:cleanText(extra.category),
    license:cleanText(extra.license),source_url:url,app_url:safeHttps(extra.app_url),icon_url:safeHttps(extra.icon_url),metadata_json:extra.metadata_json||{}};
}
function unique(records) {
  const seen=new Set();return records.filter(r=>{if(!r)return false;const k=r.source_key+"|"+r.external_id;if(seen.has(k))return false;seen.add(k);return true;});
}
function findIconPath(value,depth=0) {
  if(depth>8||value==null)return null;
  if(typeof value==="string")return /\.(png|jpe?g|webp|svg)(?:[?#].*)?$/i.test(value.trim())?value.trim():null;
  if(Array.isArray(value)){for(const item of value){const found=findIconPath(item,depth+1);if(found)return found;}return null;}
  if(typeof value==="object"){
    if(typeof value.name==="string"&&/\.(png|jpe?g|webp|svg)(?:[?#].*)?$/i.test(value.name))return value.name;
    for(const key of ["en-US","en","icon","icons","name"]){if(key in value){const found=findIconPath(value[key],depth+1);if(found)return found;}}
    for(const item of Object.values(value)){const found=findIconPath(item,depth+1);if(found)return found;}
  }
  return null;
}

async function json(url) {
  const res=await fetch(url,{headers:{"user-agent":"MTA-Applications-Engine/0.3"}});
  if(!res.ok)throw new Error("HTTP "+res.status+" from "+new URL(url).hostname);
  return res.json();
}
async function fdroid(indexUrl="https://f-droid.org/repo/index-v2.json",sourceKey="fdroid",appPageBase="https://f-droid.org/packages/",iconBase="https://f-droid.org/repo/") {
  const payload=await json(indexUrl),apps=payload.apps||payload.packages||{};
  const entries=Array.isArray(apps)?apps.map(x=>[null,x]):Object.entries(apps),out=[];
  for(const pair of entries){
    const key=pair[0],entry=pair[1];if(!entry||typeof entry!=="object")continue;
    const m=entry.metadata&&typeof entry.metadata==="object"?entry.metadata:entry,loc=m.localized||{},en=loc["en-US"]||loc.en||{};
    const val=(k)=>{let x=en[k]??loc[k]??m[k];while(x&&typeof x==="object"&&!Array.isArray(x))x=x["en-US"]??x.en??Object.values(x)[0];return x??null;};
    const id=key||entry.packageName||entry.package_name||entry.id,iconName=findIconPath(en.icon??loc.icon??m.icon??entry.icon),cats=m.categories||[];
    out.push(record(sourceKey,id,"android",val("name")||val("title")||id,appPageBase+id+(appPageBase.includes("apt.izzysoft.de")?"":"/"),
      {developer:m.authorName||m.author,summary:val("summary")||val("short_description"),description:val("description")||val("full_description"),
       category:Array.isArray(cats)?cats.join(", "):cats,license:m.license,app_url:m.webSite||m.sourceCode,
       icon_url:iconName?(String(iconName).startsWith("https://")?iconName:iconBase+String(iconName).replace(/^\/+/, "")):null,
       metadata_json:{package_name:id,source_code:m.sourceCode,issue_tracker:m.issueTracker,website:m.webSite,added:m.added||entry.added,last_updated:m.lastUpdated||entry.lastUpdated}}));
  }
  return unique(out);
}
async function homebrew() {
  const payload=await json("https://formulae.brew.sh/api/cask.json"),items=Array.isArray(payload)?payload:(payload.casks||[]);
  return unique(items.map(x=>{const token=x.token,name=Array.isArray(x.name)?x.name[0]:token;return record("homebrew-cask",token,"macos",name,"https://formulae.brew.sh/cask/"+token,
    {summary:x.desc,description:x.desc,category:x.category,license:x.license,app_url:x.homepage,icon_url:x.artwork?.icon,metadata_json:{version:x.version,homepage:x.homepage,caveats:x.caveats}});}));
}
async function apple(env,entity=null,sourceKey="apple-itunes",platform="ios") {
  const terms=(env.APPLE_SEARCH_TERMS||"browser,office,photo,video,education,security,finance,communication,developer,utilities,messaging,maps,health,weather").split(","),out=[];
  for(const term of terms){try{
    const u=new URL("https://itunes.apple.com/search");u.searchParams.set("term",term.trim());u.searchParams.set("country",env.APPLE_COUNTRY||"us");
    u.searchParams.set("limit",String(Math.max(1,Math.min(Number(env.APPLE_LIMIT||100),200))));u.searchParams.set("media","software");if(entity)u.searchParams.set("entity",entity);
    const p=await json(u.toString());
    for(const x of p.results||[]){if(x.kind&&!["software","software-package","mac-software"].includes(x.kind))continue;
      out.push(record(sourceKey,x.trackId,platform,x.trackName,x.trackViewUrl,{developer:x.artistName,summary:x.shortDescription,description:x.description,
        category:x.genres?.[0],app_url:x.trackViewUrl,icon_url:x.artworkUrl512||x.artworkUrl100,
        metadata_json:{bundle_id:x.bundleId,version:x.version,price:x.price,genres:x.genres,size_bytes:x.fileSizeBytes??null}}));}
  }catch(e){console.log("Apple search failed",term,String(e));}}
  return unique(out);
}
async function snapStore(env) {
  const terms=(env.SNAP_SEARCH_TERMS||"browser,office,photo,video,education,security,finance,communication,developer,utilities").split(","),out=[];
  for(const term of terms){try{
    const u=new URL("https://api.snapcraft.io/v2/snaps/find");u.searchParams.set("q",term.trim());
    const res=await fetch(u.toString(),{headers:{"user-agent":"MTA-Applications-Engine/0.3","Snap-Device-Series":"16","Accept":"application/json"}});
    if(!res.ok)throw new Error("HTTP "+res.status);
    const p=await res.json(),items=p.results||[];
    for(const x of items.slice(0,Math.max(1,Math.min(Number(env.SNAP_LIMIT||50),100)))){
      const publisher=x.publisher||{},media=x.media||[],icon=media.find(m=>m.type==="icon")?.url,name=x.name;
      out.push(record("snap-store",name,"linux",x.title||name,name?"https://snapcraft.io/"+name:null,{developer:publisher["display-name"],
        summary:x.summary,description:x.description,category:Array.isArray(x.categories)?x.categories.join(", "):null,icon_url:icon,
        metadata_json:{snap_name:name,snap_id:x.id,publisher:publisher}}));
    }
  }catch(e){console.log("Snap Store search failed",term,String(e));}}
  return unique(out);
}
async function steam(env) {
  const terms=(env.STEAM_SEARCH_TERMS||"action,adventure,strategy,simulation,rpg,indie,free to play,multiplayer").split(","),out=[];
  for(const term of terms){try{
    const u=new URL("https://store.steampowered.com/api/storesearch/");
    u.searchParams.set("term",term.trim());u.searchParams.set("l","english");u.searchParams.set("cc","us");
    const p=await json(u.toString()),items=p.items||[];
    for(const x of items){out.push(record("steam",x.id,"pc-games",x.name,x.id?"https://store.steampowered.com/app/"+x.id+"/":null,
      {icon_url:x.tiny_image,metadata_json:{steam_appid:x.id,item_type:x.type,price:x.price,source_scope:"storefront_search"}}));}
  }catch(e){console.log("Steam storefront search failed",term,String(e));}
    await new Promise(resolve=>setTimeout(resolve,150));
  }
  return unique(out);
}
async function archLinux(env) {
  const terms=(env.ARCH_SEARCH_TERMS||"browser,office,photo,video,education,security,finance,communication,developer,utilities").split(","),out=[];
  for(const term of terms){try{
    const u=new URL("https://archlinux.org/packages/search/json/");u.searchParams.set("q",term.trim());
    const p=await json(u.toString()),items=Array.isArray(p)?p:(p.results||p.packages||[]);
    for(const x of items.slice(0,Math.max(1,Math.min(Number(env.ARCH_LIMIT||100),200)))){
      const name=x.pkgname||x.name,repo=x.repo||"extra",arch=x.arch||"x86_64";if(!name)continue;
      const rawSize=x.compressed_size??x.pkg_size??x.download_size,size=rawSize==null?null:Number(rawSize);
      out.push(record("arch-linux",repo+"/"+arch+"/"+name,"linux",name,"https://archlinux.org/packages/"+repo+"/"+arch+"/"+name+"/",
        {developer:x.packager,summary:x.pkgdesc||x.description,description:x.pkgdesc||x.description,
         category:Array.isArray(x.groups)?x.groups.join(", "):null,license:Array.isArray(x.licenses)?x.licenses.join(", "):null,
         metadata_json:{version:x.pkgver,repository:repo,architecture:arch,size_bytes:Number.isFinite(size)?size:null,last_updated:x.last_update,homepage:x.url}}));
    }
  }catch(e){console.log("Arch Linux package search failed",term,String(e));}}
  return unique(out);
}
async function debianPackages(env) {
  const terms=(env.DEBIAN_SEARCH_TERMS||"browser,office,graphics,video,audio,education,security,games,communication,utilities").split(",").map(x=>x.trim()).filter(Boolean);
  const limit=Math.max(1,Math.min(Number(env.DEBIAN_LIMIT_PER_TERM||12),20)),names=[];
  const searches=await Promise.all(terms.map(async term=>{
    try{return await json("https://sources.debian.org/api/search/"+encodeURIComponent(term)+"/");}
    catch(e){console.log("Debian package search failed",term,String(e));return null;}
  }));
  for(const payload of searches){const results=payload?.results||{},exact=results.exact,other=Array.isArray(results.other)?results.other:[];
    if(exact?.name)names.push(String(exact.name));
    for(const item of other.slice(0,limit))if(item?.name)names.push(String(item.name));
  }
  const uniqueNames=[...new Set(names)];
  const records=await Promise.all(uniqueNames.map(async packageName=>{
    try{
      const url="https://packages.debian.org/sid/amd64/"+encodeURIComponent(packageName);
      const res=await fetch(url,{headers:{"user-agent":"MTA-Applications-Engine/0.3"}});
      if(!res.ok)return null;
      const page=await res.text();
      if(!page.toLowerCase().includes("role::program"))return null;
      const heading=page.match(/<h2[^>]*>\s*Links for [^<]*<\/h2>[\s\S]*?<h2[^>]*>([\s\S]*?)<\/h2>/i);
      if(!heading)return null;
      const clean=value=>value.replace(/<(script|style)[^>]*>[\s\S]*?<\/\1>/gi," ").replace(/<br\s*\/?>|<\/p>|<\/pre>|<\/li>/gi,"\n").replace(/<[^>]+>/g," ").replace(/&nbsp;/gi," ").replace(/&amp;/gi,"&").replace(/&lt;/gi,"<").replace(/&gt;/gi,">").replace(/&#39;|&apos;/gi,"'").replace(/&quot;/gi,'"').replace(/[ \t]+/g," ").replace(/\n\s*\n+/g,"\n").trim();
      const summary=clean(heading[1]),start=heading.index+heading[0].length,tail=page.slice(start),related=tail.search(/<h2[^>]*>\s*Other Packages Related/i);
      let body=related>=0?tail.slice(0,related):tail;
      body=body.replace(/<p[^>]*>\s*Tags:[\s\S]*$/i,"");
      const description=clean(body).slice(0,6000)||summary;
      const sizeMatch=page.match(/<th>\s*amd64\s*<\/th>\s*<td>\s*([\d,.]+)\s*(kB|MB|B)/i);
      let sizeBytes=null;if(sizeMatch){const factor={b:1,kb:1024,mb:1048576}[sizeMatch[2].toLowerCase()];sizeBytes=Math.round(Number(sizeMatch[1].replace(/,/g,""))*factor);}
      const versionMatch=page.match(/<h1>\s*Package:\s*[^<(]+\(([^)]+)\)/i);
      return record("debian-packages",packageName,"linux",packageName,url,{summary,description,app_url:url,
        metadata_json:{version:versionMatch?.[1]?.trim()||null,architecture:"amd64",size_bytes:sizeBytes,suite:"sid"}});
    }catch(e){console.log("Debian package detail failed",packageName,String(e));return null;}
  }));
  return unique(records);
}

async function winget(env) {
  const terms=(env.WINGET_SEARCH_TERMS||"browser,office,photo,video,education,security,finance,communication,developer,utilities").split(","),out=[];
  for(const term of terms){try{
    const u=new URL("https://api.winget.run/v2/packages");u.searchParams.set("query",term.trim());
    u.searchParams.set("take",String(Math.max(1,Math.min(Number(env.WINGET_LIMIT||24),100))));u.searchParams.set("page","0");
    const p=await json(u.toString()),items=Array.isArray(p)?p:(p.Packages||p.packages||p.value||[]);
    for(const x of items){const latest=x.Latest||x.latest||{},id=x.Id||x.id,name=latest.Name||x.name||id;
      out.push(record("winget-run-community",id,"windows",name,id?"https://winget.run/pkg/"+id:null,{developer:latest.Publisher||x.publisher,
        summary:latest.Description||x.description,description:latest.Description||x.description,license:latest.License||x.license,
        app_url:latest.Homepage||x.homePageUrl,icon_url:x.IconUrl||x.iconUrl,metadata_json:{version:latest.Version||x.version,tags:latest.Tags||x.tags}}));}
  }catch(e){console.log("WinGet search failed",term,String(e));}}
  return unique(out);
}
async function flathub(env) {
  try{
    const p=await json("https://flathub.org/api/v2/appstream?filter=apps&sort=alphabetical");
    let items=Array.isArray(p)?p:(p.items||p.apps||p.data||p.results||[]);
    if(items&&!Array.isArray(items)&&typeof items==="object")items=Object.entries(items).map(([id,v])=>Object.assign({id:id},v||{}));
    const ids=Array.from(new Set(items.map(x=>typeof x==="string"?x:(x.id||x.app_id||x.appId||x.flatpak_id)).filter(Boolean))).slice(0,Math.max(1,Math.min(Number(env.FLATHUB_MAX_APPS||1000),5000)));
    const out=[];
    for(let i=0;i<ids.length;i+=8){
      const batch=await Promise.all(ids.slice(i,i+8).map(async id=>{try{
        const d=await json("https://flathub.org/api/v2/appstream/"+encodeURIComponent(id)+"?locale=en"),cats=d.categories||[],shots=d.screenshots||[];
        return record("flathub",id,"linux",d.name||id,"https://flathub.org/apps/"+id,{developer:d.developer_name||d.developer,
          summary:d.summary,description:d.description,category:Array.isArray(cats)?cats.join(", "):cats,license:d.license,app_url:d.project_url,
          icon_url:d.icon||(Array.isArray(d.icons)?d.icons.find(x=>typeof x==="string"&&/^https:\/\//.test(x)):null),metadata_json:{flatpak_id:id,categories:cats}});
      }catch(e){return null;}}));out.push(...batch);
    }return unique(out);
  }catch(e){console.log("Flathub fetch failed",String(e));return [];}
}
async function curated() {
  return unique(CURATED_WEB.map(x=>record("mta-curated-web",x.id||x.name,"web",x.name,x.app_url||x.homepage,
    {developer:x.developer,summary:x.summary,description:x.description,category:x.category,app_url:x.app_url||x.homepage,
     icon_url:x.icon_url,metadata_json:{homepage:x.homepage,curated:true}})));
}
function hasValue(value) { return value!=null&&(typeof value!=="string"||value.trim().length>0); }
function mergeMetadata(previous,incoming) {
  const old=previous&&typeof previous==="object"&&!Array.isArray(previous)?previous:{};
  const fresh=incoming&&typeof incoming==="object"&&!Array.isArray(incoming)?incoming:{};
  const merged={...old};
  for(const [key,value] of Object.entries(fresh)){
    if(value&&typeof value==="object"&&!Array.isArray(value)&&merged[key]&&typeof merged[key]==="object"&&!Array.isArray(merged[key]))merged[key]=mergeMetadata(merged[key],value);
    else if(Array.isArray(value)&&value.length===0&&Array.isArray(merged[key])&&merged[key].length>0)continue;
    else if(hasValue(value)||!(key in merged))merged[key]=value;
  }
  return merged;
}
async function upsert(db,records) {
  const counts={inserted:0,updated:0,unchanged:0,skipped:0};
  const columns=["platform","name","developer","summary","description","category","license","source_url","app_url","icon_url"];
  for(const r of records){
    if(!r?.source_key||!r.external_id||!r.name||!r.source_url){counts.skipped++;continue;}
    const incoming=[r.platform,r.name,r.developer,r.summary,r.description,r.category,r.license,r.source_url,r.app_url,r.icon_url];
    const found=await db.unsafe("SELECT id,platform,name,developer,summary,description,category,license,source_url,app_url,icon_url,metadata FROM apps WHERE source_key=$1 AND external_id=$2 LIMIT 1",[r.source_key,r.external_id]);
    if(found.length){
      const old=found[0],values=incoming.map((value,index)=>hasValue(value)?value:(old[columns[index]]??null));
      const metadata=mergeMetadata(old.metadata,r.metadata_json);
      const same=columns.every((key,i)=>(old[key]??null)===(values[i]??null))&&JSON.stringify(old.metadata||{})===JSON.stringify(metadata);
      if(same){counts.unchanged++;continue;}
      await db.unsafe("UPDATE apps SET platform=$1,name=$2,developer=$3,summary=$4,description=$5,category=$6,license=$7,source_url=$8,app_url=$9,icon_url=$10,metadata=$11::json,updated_at=now() WHERE id=$12",
        [...values,JSON.stringify(metadata),old.id]);
      counts.updated++;
    }else{
      await db.unsafe("INSERT INTO apps (source_key,external_id,platform,name,developer,summary,description,category,license,source_url,app_url,icon_url,metadata) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::json) ON CONFLICT (source_key,external_id) DO NOTHING",
        [r.source_key,r.external_id,...incoming,JSON.stringify(r.metadata_json||{})]);
      counts.inserted++;
    }
  }
  return counts;
}
function isAdmin(request,env) {
  const expected=env.ADMIN_API_TOKEN||"",supplied=request.headers.get("X-Admin-Token")||"";
  if(!expected||!supplied||expected.length!==supplied.length)return false;
  let diff=0;for(let i=0;i<expected.length;i++)diff|=expected.charCodeAt(i)^supplied.charCodeAt(i);return diff===0;
}
function qualityFields(total,counts) {
  const fields={};
  for(const field of ["icon_url","description","summary","developer","category","license","app_url","source_url"]){
    const present=Number(counts[field]||0);
    fields[field]={present,missing:Math.max(total-present,0),percent:total?Math.round(present*10000/total)/100:0};
  }
  return fields;
}
async function handle(request,env) {
  const origin=corsOrigin(request,env);
  if(request.method==="OPTIONS")return new Response(null,{status:204,headers:{"access-control-allow-origin":origin,"access-control-allow-methods":"GET, POST, OPTIONS","access-control-allow-headers":"Content-Type, X-Admin-Token","vary":"Origin"}});
  const url=new URL(request.url),path=url.pathname;
  if(path==="/health"&&request.method==="GET")return response({status:"ok",service:"MTA Applications Engine",version:"0.4.0",storage:githubStorageEnabled(env)?"github-json-shards":"postgresql"},200,origin);
  if(path==="/api/v1/apps"&&request.method==="GET"){
    if(githubStorageEnabled(env)){
      const result=await githubListApps(env,url);
      return response(result.rows,200,origin,{"access-control-expose-headers":"X-Total-Count","x-total-count":String(result.total)});
    }
    const limit=Math.max(1,Math.min(Number(url.searchParams.get("limit")||50),200)),offset=Math.max(0,Number(url.searchParams.get("offset")||0));
    const clauses=[],quote=value=>"'"+String(value).replace(/'/g,"''")+"'";
    const q=url.searchParams.get("q"),platform=url.searchParams.get("platform"),source=url.searchParams.get("source");
    if(q){const term=quote("%"+q.trim()+"%");clauses.push("(name ILIKE "+term+" OR developer ILIKE "+term+" OR summary ILIKE "+term+" OR description ILIKE "+term+" OR category ILIKE "+term+")");}
    if(platform)clauses.push("platform = "+quote(platform.toLowerCase()));
    if(source)clauses.push("source_key = "+quote(source.toLowerCase()));
    const where=clauses.length?"WHERE "+clauses.join(" AND ")+" ":"";
    const db=database(env);
    const totalRows=await queryWithRetry(env,"SELECT count(*)::int AS total FROM apps "+where);
    const sql="SELECT id,source_key AS source,external_id,platform,name,developer,summary,description,category,license,source_url,app_url,icon_url,metadata AS metadata_json FROM apps "+where+"ORDER BY name,platform,source_key,external_id LIMIT "+limit+" OFFSET "+offset;
    const rows=await queryWithRetry(env,sql);
    return response(rows,200,origin,{"access-control-expose-headers":"X-Total-Count","x-total-count":String(totalRows[0]?.total||0)});
  }
  if(path==="/api/v1/catalog-stats"&&request.method==="GET"){
    if(githubStorageEnabled(env)){
      const stats=await githubJson(env,"indexes/stats.json");
      return response(stats,200,origin);
    }
    const db=database(env),results=await Promise.all([
      queryWithRetry(env,"SELECT platform,count(*)::int AS count FROM apps GROUP BY platform ORDER BY platform"),
      queryWithRetry(env,`SELECT source_key,count(*)::int AS total,\n        count(*) FILTER (WHERE icon_url IS NOT NULL AND btrim(icon_url)<>'' AND lower(icon_url) LIKE 'https://%')::int AS icon_url,\n        count(*) FILTER (WHERE description IS NOT NULL AND btrim(description)<>'')::int AS description,\n        count(*) FILTER (WHERE summary IS NOT NULL AND btrim(summary)<>'')::int AS summary,\n        count(*) FILTER (WHERE developer IS NOT NULL AND btrim(developer)<>'')::int AS developer,\n        count(*) FILTER (WHERE category IS NOT NULL AND btrim(category)<>'')::int AS category,\n        count(*) FILTER (WHERE license IS NOT NULL AND btrim(license)<>'')::int AS license,\n        count(*) FILTER (WHERE app_url IS NOT NULL AND btrim(app_url)<>'' AND lower(app_url) LIKE 'https://%')::int AS app_url,\n        count(*) FILTER (WHERE source_url IS NOT NULL AND btrim(source_url)<>'' AND lower(source_url) LIKE 'https://%')::int AS source_url\n        FROM apps GROUP BY source_key ORDER BY source_key`)
    ]);
    const fieldNames=["icon_url","description","summary","developer","category","license","app_url","source_url"];
    const qualitySources={},sources={},globalCounts=Object.fromEntries(fieldNames.map(key=>[key,0]));
    let total=0;
    for(const row of results[1]){
      const count=Number(row.total||0),counts={};total+=count;sources[row.source_key]=count;
      for(const field of fieldNames){counts[field]=Number(row[field]||0);globalCounts[field]+=counts[field];}
      qualitySources[row.source_key]={total:count,fields:qualityFields(count,counts)};
    }
    return response({total,with_icon:globalCounts.icon_url,without_icon:Math.max(total-globalCounts.icon_url,0),
      platforms:Object.fromEntries(results[0].map(x=>[x.platform,x.count])),sources,
      quality:{fields:qualityFields(total,globalCounts),sources:qualitySources},metadata_only:true},200,origin);
  }
  if((path==="/api/v1/ingest/catalogs"||path==="/api/v1/ingest/fdroid")&&request.method==="POST"){
    if(githubStorageEnabled(env))return response({detail:"GitHub-backed catalogue is read-only through the API. Run the GitHub Actions catalogue workflow to update it."},405,origin);
    if(!isAdmin(request,env))return response({detail:"Ingestion is disabled or unauthorized"},403,origin);
    const db=database(env);
    if(path==="/api/v1/ingest/fdroid"){
      let records;try{records=await fdroid();}catch(e){return response({detail:"F-Droid catalogue unavailable"},502,origin);}
      if(!records.length)return response({detail:"F-Droid returned no usable application metadata"},502,origin);
      return response({source:"F-Droid",discovered:records.length,...await upsert(db,records),note:"Metadata only. No APK files were downloaded, mirrored, or hosted."},200,origin);
    }
    const tasks={fdroid:fdroid(),"izzyondroid":fdroid("https://apt.izzysoft.de/fdroid/repo/index-v2.json","izzyondroid","https://apt.izzysoft.de/fdroid/index/apk/","https://apt.izzysoft.de/fdroid/repo/"),"snap-store":snapStore(env),flathub:flathub(env),"homebrew-cask":homebrew(),"apple-itunes":apple(env),"apple-macos":apple(env,"macSoftware","apple-macos","macos"),"winget-run-community":winget(env),"arch-linux":archLinux(env),"debian-packages":debianPackages(env),steam:steam(env),"mta-curated-web":curated()};
    const entries=await Promise.all(Object.entries(tasks).map(async pair=>[pair[0],await pair[1].catch(e=>{console.log("Catalogue failed",pair[0],String(e));return []})]));
    const summary={};
    for(const entry of entries){const key=entry[0],records=entry[1];try{summary[key]=Object.assign({fetched:records.length},await upsert(db,records));}catch(e){console.log("Database write failed",key,String(e));summary[key]={fetched:records.length,error:"Database write failed"};}}
    return response({status:"completed",sources:summary,note:"Metadata only. No installer files were downloaded, mirrored, or hosted."},200,origin);
  }
  return response({detail:"Not Found"},404,origin);
}
export default {async fetch(request,env){try{return await handle(request,env);}catch(error){console.error("MTA Applications Engine request failed",error);return response({detail:"Internal server error"},500,corsOrigin(request,env));}}};
