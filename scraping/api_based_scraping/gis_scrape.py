'''
scraping using New York Geographic Information Gateway
Some photos are uploaded from websites like MyCoast
Should contain geo and image information
'''
import requests
import json

def get_nyc_flood_photos():
    # connect with url
    # MyCoast NY reports, published by NYSDOS_GIS (not the NYSDEC org)
    base_url = "https://services1.arcgis.com/tikbh7xC3WJpzTz6/ArcGIS/rest/services/MyCoast_NY_new/FeatureServer/0/query"

    # 2. Define the Query Parameters
    params = {
        "where": "1=1",
        "geometry": "-74.259,40.477,-73.700,40.917", # NYC bounding box
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",               # layer is Web Mercator; without this the bbox matches 0
        "outSR": "4326",
        "outFields": "ID,Title,Report_Type,Date,Latitude,Longitude,ImageUrls,Report_URL",
        "orderByFields": "ObjectId",  # stable order so paging does not skip/repeat
        #"resultRecordCount": 1000,    # layer maxRecordCount
        "f": "json"
    }

    print("Querying NY Geographic Information Gateway...")

    # 3. Make the API Request, paging past maxRecordCount
    features, offset = [], 0
    while True:
        response = requests.get(base_url, params={**params, "resultOffset": offset}, timeout=60)
        response.raise_for_status()
        data = response.json()
        if "error" in data:           # ArcGIS reports errors with HTTP 200
            raise RuntimeError(f"ArcGIS query failed: {data['error']}")
        features += data.get("features", [])
        if not data.get("exceededTransferLimit"):
            break
        offset += len(data["features"])

    print(f"Found {len(features)} flood reports in NYC!\n")

    # 4. Loop through the results and print the Image URLs
    for index, feature in enumerate(features):
        attributes = feature.get('attributes', {})
        image_urls_string = attributes.get('ImageUrls')

        # Multiple image URLs come in one string, separated by '|'
        urls = [u.strip() for u in (image_urls_string or "").split("|") if u.strip()]
        if urls:
            print(f"Report {index + 1}:")
            print(f"Date: {attributes.get('Date')}")  # epoch milliseconds
            for url in urls:
                print(f"Photo: {url}")
            print("-" * 20)

    return features

# Run the function
if __name__ == "__main__":
    get_nyc_flood_photos()
