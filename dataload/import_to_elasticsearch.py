import json
import argparse
import flattentool
import shutil
import uuid
import tempfile
import os
import csv
import gzip
from pprint import pprint
import elasticsearch.helpers
import requests
import time
import ijson


ES_INDEX = os.environ.get("ES_INDEX", "threesixtygiving")

id_name_org_mappings = {"fundingOrganization": {}, "recipientOrganization": {}}
name_duplicates = [["file_name", "org_type", "org_id", "first_name", "duplicate_name"]]
bad_org_ids = []

postcode_to_area = {}
district_code_to_area = {}
ward_code_to_area = {}
current_dir = os.path.dirname(os.path.realpath(__file__))


def convert_spreadsheet(file_path, file_type, tmp_dir):
    #file_type = file_name.split('.')[-1]
    encoding = 'utf-8'
    converted_path = os.path.join(tmp_dir, 'output.json')
    if file_type == 'csv':
        destination = os.path.join(tmp_dir, 'grants.csv')
        shutil.copy(file_path, destination)
        try:
            with open(destination, encoding='utf-8') as main_sheet_file:
                main_sheet_file.read()
        except UnicodeDecodeError:
            try:
                with open(destination, encoding='cp1252') as main_sheet_file:
                    main_sheet_file.read()
                encoding = 'cp1252'
            except UnicodeDecodeError:
                encoding = 'latin_1'
        input_name = tmp_dir
    else:
        input_name = file_path
    try:
        flattentool.unflatten(
            input_name,
            output_name=converted_path,
            input_format=file_type,
            main_sheet_name='grants',
            root_id='',
            schema='https://raw.githubusercontent.com/ThreeSixtyGiving/standard/master/schema/360-giving-schema.json',
            convert_titles=True,
            encoding=encoding
        )
    except Exception:
        print("Unflattening failed for file {}".format(file_path))
        raise

# curl http://test-360giving.pantheon.io/api/3/action/current_package_list_with_resources | grep -Eo '[^"]+\.json' | sed 's/\\\//\//g' | while read url; do wget "$url"; done


def import_to_elasticsearch(files, clean):

    es = elasticsearch.Elasticsearch()

    # Delete the index
    if clean:
        result = es.indices.delete(index=ES_INDEX, ignore=[404])
        pprint(result)

    # Add the extra mapping info we want
    # (the rest will be auto inferred from the data we feed in)
    mappings = {
        "grant": {
            "_all": {
                "analyzer": "english"
            },
            "properties": {
                "id": {"type": "string", "index": "not_analyzed"},
                "filename": {"type": "string", "index": "not_analyzed"},
                "recipientRegionName": {"type": "string", "index": "not_analyzed"},
                "recipientDistrictName": {"type": "string", "index": "not_analyzed"},
                "recipientWardName": {"type": "string", "index": "not_analyzed"},
                "recipientLocation": {"type": "string"},
                "awardDate": {
                    "type": "date",
                    "ignore_malformed": True
                },
                "awardDatdateModifiede": {"type": "string", "index": "not_analyzed"},
                "dateModified": {"type": "string", "index": "not_analyzed"},
                "plannedDates": {
                    "properties": {
                        "startDate": {"type": "string", "index": "not_analyzed"},
                        "endDate": {"type": "string", "index": "not_analyzed"},
                        "duration": {"type": "string"}
                    }
                },
                "recipientOrganization": {
                    "properties": {
                        "addressLocality": {
                            "type": "string", "index": "not_analyzed"
                        },
                        "charityNumber": {
                            "type": "string", "index": "not_analyzed"
                        },
                        "companyNumber": {
                            "type": "string", "index": "not_analyzed"
                        },
                        "id": {
                            "type": "string", "index": "not_analyzed"
                        },
                        "url": {
                            "type": "string", "index": "not_analyzed"
                        },
                        "name": {
                            "type": "string", "analyzer": "english",
                        },
                        "streetAddress": {
                            "type": "string", "analyzer": "english",
                        },
                        "id_and_name": {
                            "type": "string", "index": "not_analyzed"
                        }
                    }
                },
                "fundingOrganization": {
                    "properties": {
                        "addressLocality": {
                            "type": "string", "index": "not_analyzed"
                        },
                        "charityNumber": {
                            "type": "string", "index": "not_analyzed"
                        },
                        "companyNumber": {
                            "type": "string", "index": "not_analyzed"
                        },
                        "id": {
                            "type": "string", "index": "not_analyzed"
                        },
                        "url": {
                            "type": "string", "index": "not_analyzed"
                        },
                        "name": {
                            "type": "string", "analyzer": "english",
                        },
                        "streetAddress": {
                            "type": "string", "analyzer": "english",
                        },
                        "id_and_name": {
                            "type": "string", "index": "not_analyzed"
                        }
                    }
                }
            }
        }
    }

    settings = {"max_result_window": 500000}

    # Create it again
    result = es.indices.create(index=ES_INDEX, body={"mappings": mappings, "settings": settings}, ignore=[400])
    if 'error' in result and result['error']['reason'] == 'already exists':
        print('Updating existing index')
    else:
        pprint(result)

    time.sleep(1)

    with open(os.path.join(current_dir, 'charity_names.json')) as fd:
        charity_names = json.load(fd)
    id_name_org_mappings["recipientOrganization"].update(charity_names)

    get_mapping_from_index(es)

    for file_name in files:
        tmp_dir = tempfile.mkdtemp()
        if file_name.startswith('http'):
            content = requests.get(file_name).content
            new_filename = file_name.split('/')[-1].split('?')[0]
            downloaded_filename = os.path.join(tmp_dir, new_filename)
            with open(downloaded_filename, 'wb+') as downloaded_file:
                downloaded_file.write(content)
            file_name = downloaded_filename

        file_type = file_name.split('.')[-1]

        if file_type == 'json':
            json_file_name = file_name
        elif file_type in ('csv', 'xlsx'):
            json_file_name = os.path.join(tmp_dir, 'output.json')
            convert_spreadsheet(file_name, file_type, tmp_dir)
        elif file_type in ('report'):
            continue
        else:
            print('unimportable file {} (bad) file type'.format(file_name))
            continue

        def grant_generator():
            with open(json_file_name) as fp:
                stream = ijson.items(fp, 'grants.item')
                for grant in stream:
                    grant['filename'] = file_name.strip('./')
                    grant['_id'] = str(uuid.uuid4())
                    grant['_index'] = ES_INDEX
                    grant['_type'] = 'grant'
                    update_doc_with_org_mappings(grant, "fundingOrganization", file_name)
                    update_doc_with_org_mappings(grant, "recipientOrganization", file_name)
                    update_doc_with_region(grant)
                    yield grant

        pprint(file_name)
        result = elasticsearch.helpers.bulk(es, grant_generator(), raise_on_error=False)
        pprint(result)

        shutil.rmtree(tmp_dir)


def get_mapping_from_index(es):
    QUERY = {"query": {"match_all": {}},
             "aggs": {
                 "fundingOrganization": {"terms": {"field": "fundingOrganization.id_and_name", "size": 0}},
                 "recipientOrganization": {"terms": {"field": "recipientOrganization.id_and_name", "size": 0}}}}
    results = es.search(body=QUERY, index=ES_INDEX)
    for bucket in results["aggregations"]["fundingOrganization"]["buckets"]:
        id_name = json.loads(bucket["key"])
        id_name_org_mappings["fundingOrganization"][id_name[0]] = id_name[1]

    for bucket in results["aggregations"]["recipientOrganization"]["buckets"]:
        id_name = json.loads(bucket["key"])
        id_name_org_mappings["recipientOrganization"][id_name[0]] = id_name[1]


def add_area_to_grant(area, grant):
    if area.get('ward_name'):
        grant['recipientWardName'] = area['ward_name']
    if area['district_name']:
        grant['recipientDistrictName'] = area['district_name']
    if area['area_name']:
        grant['recipientRegionName'] = area['area_name']

    grant['recipientLocation'] = ' '.join(area.values())


def update_doc_with_region(grant):
    try:
        post_code = grant['recipientOrganization'][0]['postalCode']
    except (KeyError, IndexError):
        post_code = ''

    # test postcode first
    area = postcode_to_area.get(str(post_code).replace(' ', '').upper())
    if area:
        add_area_to_grant(area, grant)
        return

    if not area:
        try:
            locations = grant['recipientOrganization'][0]['location']
        except (KeyError, IndexError):
            return

        # then test ward
        for location in locations:
            geoCode = location.get('geoCode')
            if geoCode and geoCode in ward_code_to_area:
                add_area_to_grant(ward_code_to_area.get(geoCode), grant)
                return

        # finally district
        for location in locations:
            geoCode = location.get('geoCode')
            if geoCode and geoCode in district_code_to_area:
                add_area_to_grant(district_code_to_area.get(geoCode), grant)
                return
            # No NI data but try and get name from data
            if geoCode and geoCode.startswith("N09"):
                grant['recipientRegionName'] = "Northern Ireland"
                grant['recipientDistrictName'] = location["name"]


def update_doc_with_org_mappings(grant, org_key, file_name):
    mapping = id_name_org_mappings[org_key]
    orgs = grant.get(org_key, [])
    for org in orgs:
        org_id, name = org.get('id'), org.get('name')
        if not name:
            name = org_id
        if not org_id:
            return
        if '/' in org_id:
            bad_org_ids.append([file_name, org_key, org_id])

        found_name = mapping.get(org_id)
        if found_name:
            if found_name != name:
                name_duplicates.append([file_name, org_key, org_id, found_name, name])
        else:
            mapping[org_id] = name
            found_name = name
        org["id_and_name"] = json.dumps([found_name, org_id])


def get_area_mappings():
    with open(os.path.join(current_dir, 'codelist.csv')) as codelist, gzip.open(os.path.join(current_dir, 'codepoint_with_heading.csv.gz'), 'rt') as codepoint:
        codelist_csv = csv.DictReader(codelist)
        code_to_name = {}
        for row in codelist_csv:
            code_to_name[row['code']] = row['name']
        
        codepoint_csv = csv.DictReader(codepoint)

        for row in codepoint_csv:
            district_code = row['Admin_district_code']
            district_name = code_to_name.get(district_code, '')
            ward_code = row['Admin_ward_code']
            ward_name = code_to_name.get(ward_code, '')

            regional_code = row['NHS_HA_code']
            area_name = ''
            if not regional_code or regional_code[0] != 'E':
                country_code = row['Country_code']
                first_letter = country_code[0]
                if first_letter == 'S':
                    area_name = 'Scotland'
                if first_letter == 'W':
                    area_name = 'Wales'
            else:
                area_name = code_to_name[regional_code]

            postcode_to_area[row['Postcode'].replace(' ', '').upper()] = {
                'district_name': district_name, 'area_name': area_name, 'ward_name': ward_name
            }
            district_code_to_area[district_code] = {
                'district_name': district_name, 'area_name': area_name
            }
            ward_code_to_area[ward_code] = {
                'district_name': district_name, 'area_name': area_name, 'ward_name': ward_name
            }

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Import 360 files in a directory to elasticsearch')
    parser.add_argument('--clean', help='files to import', action='store_true')
    parser.add_argument('--reports', help='files to import', action='store_true')
    parser.add_argument('files', help='files to import', nargs='+')
    args = parser.parse_args()
    get_area_mappings()
    import_to_elasticsearch(args.files, args.clean)
    if args.reports:
        with open("differing_names.csv.report", "w+") as differing_names_file:
            csv_writer = csv.writer(differing_names_file)
            csv_writer.writerows(name_duplicates)
        with open("bad_org_ids.csv.report", "w+") as bad_org_ids_file:
            csv_writer = csv.writer(bad_org_ids_file)
            csv_writer.writerows(bad_org_ids)
