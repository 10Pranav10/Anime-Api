import asyncio
import time
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query, APIRouter
import uvicorn
from typing import Optional
from fastapi.middleware.cors import CORSMiddleware

# ==========================================
# 1. API INITIALIZATION & CONFIG
# ==========================================

app = FastAPI(
    title="AnimeSalt Fast API",
    description="A high-performance, async scraping API with concurrent fetching and caching.",
    version="4.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods (GET, POST, etc.)
    allow_headers=["*"],  # Allows all headers
)

BASE_URL = "https://animesalt.ac/"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# Simple In-Memory Cache (5 minute TTL) to prevent spamming the target server
CACHE = {}
CACHE_TTL = 300  

# Global HTTPX Client for connection pooling
http_client = httpx.AsyncClient(headers=HEADERS, timeout=15.0)

@app.on_event("shutdown")
async def shutdown_event():
    await http_client.aclose()

# ==========================================
# 2. ASYNC NETWORK & CACHE LAYER
# ==========================================

async def fetch_html(url: str, params: Optional[dict] = None) -> str:
    """Fetches HTML asynchronously with a built-in TTL cache."""
    cache_key = f"{url}_{str(params)}"
    
    # Return cached HTML if it exists and is fresh
    if cache_key in CACHE and (time.time() - CACHE[cache_key]['timestamp']) < CACHE_TTL:
        return CACHE[cache_key]['html']

    response = await http_client.get(url, params=params)
    if response.status_code == 404:
        return "" # Handle 404s gracefully
    response.raise_for_status()
    
    html_data = response.text
    CACHE[cache_key] = {'html': html_data, 'timestamp': time.time()}
    return html_data

# ==========================================
# 3. FAST HTML PARSERS (Using lxml)
# ==========================================

def parse_post_article(article):
    link_tag = article.find('a', class_='lnk-blk')
    if not link_tag: return None
    
    img_tag = article.find('img')
    image_url, title = "", ""
    
    if img_tag:
        image_url = img_tag.get('data-src') or img_tag.get('src', '')
        if image_url.startswith('//'): image_url = 'https:' + image_url
            
        alt_text = img_tag.get('alt', '')
        title = alt_text[6:].strip() if alt_text.lower().startswith("image ") else alt_text.strip()

    title_tag = article.find('h2', class_='entry-title')
    if title_tag and title_tag.text.strip(): title = title_tag.text.strip()
    if not title: title = "Unknown Title"

    season_tag = article.find('span', class_='post-ql')
    ep_tag = article.find('span', class_='year')
    
    return {
        "title": title,
        "link": link_tag.get('href', ''),
        "image": image_url,
        "season": season_tag.text.strip() if season_tag and season_tag.text.strip() else "",
        "episode": ep_tag.text.strip() if ep_tag and ep_tag.text.strip() else ""
    }

def parse_chart_item(item):
    link_tag = item.find('a', class_='chart-poster')
    if not link_tag: return None
    
    img_tag = link_tag.find('img')
    image_url = ""
    if img_tag:
        image_url = img_tag.get('data-src') or img_tag.get('src', '')
        if image_url.startswith('//'): image_url = 'https:' + image_url
            
    title_tag = item.find('div', class_='chart-title')
    rank_tag = item.find('div', class_='chart-number')
    
    return {
        "rank": rank_tag.text.strip() if rank_tag else "",
        "title": title_tag.text.strip() if title_tag else "Unknown Title",
        "link": link_tag.get('href', ''),
        "image": image_url
    }

def extract_categorized_anime(html_content):
    soup = BeautifulSoup(html_content, 'lxml')
    categorized_data = {}
    
    for header in soup.find_all('h3', class_='section-title'):
        category_name = header.text.replace('View More »', '').replace('\n', '').strip()
        if not category_name: continue
            
        if "Most-Watched" in category_name:
            container = header.find_next_sibling('div', class_='aa-cn')
            if container:
                items = container.find_all('div', class_='chart-item')
                categorized_data[category_name] = [parse_chart_item(item) for item in items if parse_chart_item(item)]
        else:
            parent_section = header.find_parent('section')
            if parent_section:
                articles = parent_section.find_all('article', class_='post')
                seen_links, unique_articles = set(), []
                for art in articles:
                    parsed_data = parse_post_article(art)
                    if parsed_data and parsed_data['link'] not in seen_links:
                        seen_links.add(parsed_data['link'])
                        unique_articles.append(parsed_data)
                categorized_data[category_name] = unique_articles

    return categorized_data

def extract_video_servers(soup):
    servers = []
    for idx, btn in enumerate(soup.find_all('div', class_='server-btn')):
        name_tag = btn.find('div', class_='server-name')
        info_tag = btn.find('div', class_='server-info')
        option_div = soup.find('div', id=f'options-{idx}')
        
        iframe_url = ""
        if option_div and option_div.find('iframe'):
            iframe = option_div.find('iframe')
            iframe_url = iframe.get('data-src') or iframe.get('src') or ""
                
        servers.append({
            "server": name_tag.text.strip() if name_tag else f"Server {idx+1}",
            "info": info_tag.text.strip() if info_tag else "",
            "iframe_url": iframe_url
        })
    return servers

def parse_episodes_from_html(container):
    episodes = []
    if not container: return episodes
        
    for article in container.find_all('article', class_='episodes'):
        ep_link_tag = article.find('a', class_='lnk-blk')
        ep_img_tag = article.find('img')
        ep_num_tag = article.find('span', class_='num-epi')
        ep_title_tag = article.find('h2', class_='entry-title')
        
        if ep_link_tag:
            img_url = ep_img_tag.get('data-src') or ep_img_tag.get('src') or '' if ep_img_tag else ""
            if img_url.startswith('//'): img_url = 'https:' + img_url
            
            episodes.append({
                "episode_number": ep_num_tag.text.strip() if ep_num_tag else "",
                "title": ep_title_tag.text.strip() if ep_title_tag else "",
                "image": img_url,
                "link": ep_link_tag.get('href', '')
            })
    return episodes

# ==========================================
# 4. HIGH-PERFORMANCE ASYNC EXTRACTORS
# ==========================================

async def fetch_single_season(ajax_url, season_id, season_name):
    """Fetches a single season's episodes asynchronously."""
    try:
        html = await fetch_html(ajax_url)
        if html:
            ep_soup = BeautifulSoup(html, 'lxml')
            return {"season_name": season_name, "season_id": season_id, "episodes": parse_episodes_from_html(ep_soup)}
    except Exception:
        pass
    return None

async def fetch_all_seasons_concurrently(soup):
    """Fires all season AJAX requests simultaneously."""
    seasons_data = []
    season_btns = soup.find_all('a', class_='season-btn')
    
    if not season_btns:
        episodes = parse_episodes_from_html(soup.find('ul', id='episode_by_temp'))
        if episodes: seasons_data.append({"season_name": "Default", "season_id": "1", "episodes": episodes})
        return seasons_data
        
    # Build tasks for concurrent execution
    tasks = []
    for btn in season_btns:
        s_id, p_id, s_name = btn.get('data-season'), btn.get('data-post'), btn.text.strip()
        ajax_url = f"{BASE_URL}wp-admin/admin-ajax.php?action=action_select_season&season={s_id}&post={p_id}"
        tasks.append(fetch_single_season(ajax_url, s_id, s_name))
        
    # Execute all requests at the exact same time
    results = await asyncio.gather(*tasks)
    
    # Filter out any failed requests
    return [r for r in results if r is not None]

async def extract_anime_details_async(html_content):
    soup = BeautifulSoup(html_content, 'lxml')
    
    title_tag = soup.find('h1')
    title = title_tag.text.strip() if title_tag else "Unknown Title"
    
    img_tag = soup.find('img', style=lambda v: v and '14rem' in v)
    image_url = img_tag.get('data-src') or img_tag.get('src') or '' if img_tag else ""
    if image_url.startswith('//'): image_url = 'https:' + image_url
            
    stats_container = soup.find(lambda t: t.name == 'div' and t.get('style') and 'margin-top: 12px' in t.get('style'))
    stats = [st.text.strip() for st in stats_container.find_all('div')] if stats_container else []
            
    overview_div = soup.find('div', id='overview-text')
    overview = overview_div.text.strip() if overview_div else ""
    
    genres, languages = [], []
    for h4 in soup.find_all('h4'):
        sibling_div = h4.find_next_sibling('div')
        if sibling_div:
            links = [a.text.strip() for a in sibling_div.find_all('a')]
            if h4.text.strip().lower() == 'genres': genres = links
            elif h4.text.strip().lower() == 'languages': languages = links

    video_servers = extract_video_servers(soup)
    is_movie = len(video_servers) > 0

    recommended = []
    rec_header = soup.find('h3', string=lambda t: t and 'Recommended' in t)
    if rec_header and rec_header.find_parent('section'):
        for art in rec_header.find_parent('section').find_all('article', class_='post'):
            parsed = parse_post_article(art)
            if parsed:
                parsed.pop('season', None); parsed.pop('episode', None)
                recommended.append(parsed)

    base_data = {
        "title": title, "image": image_url, "is_movie": is_movie, "overview": overview,
        "stats": stats, "genres": genres, "languages": languages, "recommended": recommended
    }
    
    if is_movie:
        base_data["video_servers"] = video_servers
    else:
        # Await the highly concurrent season fetcher
        base_data["seasons"] = await fetch_all_seasons_concurrently(soup)

    return base_data

def extract_episode_streaming_data(html_content):
    soup = BeautifulSoup(html_content, 'lxml')
    title_tag = soup.find('h1')
    ep_info_tag = title_tag.find_next_sibling('div') if title_tag else None
    
    current_url = soup.find('link', rel='canonical')
    current_url = current_url.get('href', '') if current_url else ""
    nav_tags = soup.find_all('a', href=lambda h: h and '/episode/' in h)

    return {
        "series_title": title_tag.text.strip() if title_tag else "Unknown",
        "episode_info": ep_info_tag.text.strip() if ep_info_tag else "Unknown",
        "servers": extract_video_servers(soup),
        "related_episodes": list(set([a['href'] for a in nav_tags if a['href'] != current_url]))
    }

# ==========================================
# 5. ASYNC ROUTE HANDLERS
# ==========================================

async def handle_paginated_request(path_prefix: str, page: int, query_params: Optional[dict] = None):
    path_prefix = path_prefix.strip("/")
    path = f"{path_prefix}/page/{page}/" if page > 1 and path_prefix else f"{path_prefix}/" if path_prefix else (f"page/{page}/" if page > 1 else "")
    target_url = f"{BASE_URL}{path}"
    
    try:
        html = await fetch_html(target_url, query_params)
        if not html:
            return {"status": "success", "current_page": page, "has_next_page": False, "data": []}
            
        soup = BeautifulSoup(html, 'lxml')
        
        item_list = []
        grid = soup.find('ul', class_='post-lst')
        if grid:
            for art in grid.find_all('article', class_='post'):
                parsed = parse_post_article(art)
                if parsed:
                    if not parsed.get('season'): parsed.pop('season', None)
                    if not parsed.get('episode'): parsed.pop('episode', None)
                    item_list.append(parsed)
        
        nav = soup.find('nav', class_='pagination')
        has_next = bool(nav and nav.find('a', string=lambda t: t and 'NEXT' in t.upper()))

        return {"status": "success", "current_page": page, "has_next_page": has_next, "total_results": len(item_list), "data": item_list}
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def handle_details_request(url: str):
    if not url.startswith(BASE_URL): return {"status": "error", "message": f"URL must start with {BASE_URL}"}
    try:
        html = await fetch_html(url)
        data = await extract_anime_details_async(html)
        return {"status": "success", "data": data}
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def handle_episode_request(url: str):
    if not url.startswith(BASE_URL): return {"status": "error", "message": f"URL must start with {BASE_URL}"}
    try:
        html = await fetch_html(url)
        return {"status": "success", "data": extract_episode_streaming_data(html)}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ==========================================
# 6. FASTAPI ROUTERS
# ==========================================

core_router = APIRouter(tags=["Core System"])
series_router = APIRouter(prefix="/api/series", tags=["General Series"])
movies_router = APIRouter(prefix="/api/movies", tags=["General Movies"])
anime_router = APIRouter(prefix="/api/anime", tags=["Anime Hub"])
cartoon_router = APIRouter(prefix="/api/cartoon", tags=["Cartoon Hub"])


# --- CORE ROUTER ---
@core_router.get("/")
async def read_root():
    return {"message": "AnimeSalt Ultra-Fast API is running!"}

@core_router.get("/api/home")
async def get_home_anime():
    try:
        html = await fetch_html(BASE_URL)
        data = extract_categorized_anime(html)
        return {"status": "success", "categories_found": list(data.keys()), "data": data}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@core_router.get("/api/search")
async def search_anime(q: str = Query(...), page: int = Query(1, ge=1)):
    return await handle_paginated_request("", page, query_params={"s": q})

# --- SERIES ROUTER ---
@series_router.get("")
@series_router.get("/")
async def get_general_series(page: int = Query(1, ge=1)):
    return await handle_paginated_request("series", page)

@series_router.get("/details")
async def get_series_details(url: str = Query(...)):
    return await handle_details_request(url)

@series_router.get("/episode")
async def get_series_episode(url: str = Query(...)):
    return await handle_episode_request(url)

# --- MOVIES ROUTER ---
@movies_router.get("")
@movies_router.get("/")
async def get_general_movies(page: int = Query(1, ge=1)):
    return await handle_paginated_request("movies", page)

@movies_router.get("/details")
async def get_movie_details(url: str = Query(...)):
    return await handle_details_request(url)

# --- ANIME HUB ROUTER ---
@anime_router.get("")
@anime_router.get("/")
async def get_anime_all(page: int = Query(1, ge=1)):
    return await handle_paginated_request("category/type/anime", page)

@anime_router.get("/movies")
async def get_anime_movies(page: int = Query(1, ge=1)):
    return await handle_paginated_request("category/type/anime", page, query_params={"type": "movies"})

@anime_router.get("/series")
async def get_anime_series(page: int = Query(1, ge=1)):
    return await handle_paginated_request("category/type/anime", page, query_params={"type": "series"})

@anime_router.get("/details")
async def get_anime_details(url: str = Query(...)):
    return await handle_details_request(url)

@anime_router.get("/episode")
async def get_anime_episode(url: str = Query(...)):
    return await handle_episode_request(url)

# --- CARTOON HUB ROUTER ---
@cartoon_router.get("")
@cartoon_router.get("/")
async def get_cartoon_all(page: int = Query(1, ge=1)):
    return await handle_paginated_request("category/type/cartoon", page)

@cartoon_router.get("/movies")
async def get_cartoon_movies(page: int = Query(1, ge=1)):
    return await handle_paginated_request("category/type/cartoon", page, query_params={"type": "movies"})

@cartoon_router.get("/series")
async def get_cartoon_series(page: int = Query(1, ge=1)):
    return await handle_paginated_request("category/type/cartoon", page, query_params={"type": "series"})

@cartoon_router.get("/details")
async def get_cartoon_details(url: str = Query(...)):
    return await handle_details_request(url)

@cartoon_router.get("/episode")
async def get_cartoon_episode(url: str = Query(...)):
    return await handle_episode_request(url)

app.include_router(core_router)
app.include_router(series_router)
app.include_router(movies_router)
app.include_router(anime_router)
app.include_router(cartoon_router)

# Note: Vercel serverless environments don't use this `__main__` block, 
# but leaving it here makes it possible to run locally!
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
