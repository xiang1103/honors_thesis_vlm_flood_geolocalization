import json 
from pathlib import Path

def retrieve_json_files(dir_path):
    '''
    return all json files inside the given directory 
    '''
    target_dir = Path(dir_path)
    json_files = list(target_dir.rglob('*.json'))
    
    if not json_files:
        return []
    return json_files

def count_news(file_path): 
    '''
    count total number of news in a json file 
    ''' 
    with open(file_path, 'r', encoding='utf-8') as file:
        data = json.load(file)
        if isinstance(data, (list, dict)):
            return len(data)
        return 0 


if __name__ == "__main__":
    files =retrieve_json_files("/home/liu47/vlm_flood/data/outlets")
    total =0 
    for f in files: 
        total += count_news(f) 

    print(total)