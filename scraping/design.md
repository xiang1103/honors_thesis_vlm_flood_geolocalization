# Design Doc for scraping news sources 

## Sources to scrape: 
- GDELT (news + image API) 
- API for news: NewsAPI.org, GNews, Mediastack 
- RSS sites (AP News, Reuters, BBC) 
- The Guardian (open API) 
- local small newspaper sites which are easier to scrape 


## Extract News (per-site) 
- trafilatura + newspaper3k 


### Failure models of extraction 
- gallery, carousel view images are not captured 
    - don't worry about non-static (JS-rendering), lazily loaded images
- failed to extract the captions for each image 

## Dataset 
```  
{
        [
            {
                title: str, 
                date: str, 
                all_text: str,  
                image_links:[] 
            }

        ]
    }
```