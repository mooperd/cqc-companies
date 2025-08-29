#!/usr/bin/env python3

import os
import csv
import sys
import time
from datetime import datetime
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from model import db, Provider, Facility

load_dotenv()

def count_csv_rows(csv_file):
    print("Counting rows in locations CSV file...")
    start_time = time.time()
    try:
        with open(csv_file, 'r', encoding='utf-8') as f:
            row_count = sum(1 for _ in f) - 1  # Subtract header row
        elapsed = time.time() - start_time
        print(f"Row counting completed in {elapsed:.2f} seconds")
        return row_count
    except IOError as e:
        print(f"ERROR: Failed to read file: {e}")
        sys.exit(1)

def load_facilities_by_location_id(session):
    """Load all facilities indexed by CQC Location ID"""
    print("Loading facilities indexed by CQC Location ID...")
    start_time = time.time()
    
    facilities = session.query(Facility).filter(Facility.cqc_location_id.isnot(None)).all()
    facility_map = {}
    
    for facility in facilities:
        location_id = facility.cqc_location_id.strip()
        if location_id:
            facility_map[location_id] = facility
    
    elapsed = time.time() - start_time
    print(f"Loaded {len(facility_map)} facilities with location IDs in {elapsed:.2f}s")
    return facility_map

def load_providers_by_cqc_id(session):
    """Load all providers indexed by CQC Provider ID"""
    print("Loading providers indexed by CQC Provider ID...")
    start_time = time.time()
    
    providers = session.query(Provider).filter(Provider.cqc_provider_id.isnot(None)).all()
    provider_map = {}
    
    for provider in providers:
        cqc_id = provider.cqc_provider_id.strip()
        if cqc_id:
            provider_map[cqc_id] = provider
    
    elapsed = time.time() - start_time
    print(f"Loaded {len(provider_map)} providers with CQC IDs in {elapsed:.2f}s")
    return provider_map

def create_missing_provider(session, provider_id, provider_name):
    """Create a new provider from location data"""
    provider = Provider(
        name=provider_name,
        cqc_provider_id=provider_id,
        website='',
        email_address='',
        phone_number='',
        address_1='',
        address_2='',
        town_city='',
        county='',
        postcode=''
    )
    session.add(provider)
    session.flush()
    return provider

def create_missing_facility(session, location_id, location_name, provider_id):
    """Create a new facility from location data"""
    facility = Facility(
        name=location_name,
        cqc_location_id=location_id,
        provider_id=provider_id,
        address_1='',
        address_2='',
        town_city='',
        county='',
        postcode='',
        phone_number='',
        website='',
        local_authority='',
        region='',
        report_publication_date='',
        url='',
        also_known_as='',
        specialisms_services='',
        service_types='',
        email_address=''
    )
    session.add(facility)
    session.flush()
    return facility

def parse_bed_count(beds_str):
    """Parse bed count from string, return None if not a number"""
    if not beds_str or beds_str.strip() == '':
        return None
    try:
        return int(beds_str.strip())
    except ValueError:
        return None

def enrich_facilities(csv_file, session, total_rows):
    facility_map = load_facilities_by_location_id(session)
    provider_map = load_providers_by_cqc_id(session)
    
    enriched_count = 0
    created_providers = 0
    created_facilities = 0
    not_found_count = 0
    start_time = time.time()
    batch_start_time = time.time()
    
    print(f"Starting location enrichment at {datetime.now().strftime('%H:%M:%S')}")
    
    try:
        with open(csv_file, 'r', encoding='utf-8') as f:
            csv_reader = csv.DictReader(f)
            
            for i, row in enumerate(csv_reader, 1):
                location_id = row.get('Location ID', '').strip()
                provider_id = row.get('Provider ID', '').strip()
                location_name = row.get('Location Name', '').strip()
                provider_name = row.get('Provider Name', '').strip()
                
                if not location_id:
                    not_found_count += 1
                    continue
                
                # Find or create provider
                provider = None
                if provider_id and provider_id in provider_map:
                    provider = provider_map[provider_id]
                elif provider_id and provider_name:
                    # Create missing provider
                    try:
                        provider = create_missing_provider(session, provider_id, provider_name)
                        provider_map[provider_id] = provider
                        created_providers += 1
                        if i <= 10:
                            print(f"  Created missing provider: {provider_name}")
                    except Exception as e:
                        print(f"ERROR: Failed to create provider {provider_name}: {e}")
                        session.rollback()
                        continue
                
                # Find or create facility
                facility = facility_map.get(location_id)
                if not facility and provider and location_name:
                    # Create missing facility
                    try:
                        facility = create_missing_facility(session, location_id, location_name, provider.id)
                        facility_map[location_id] = facility
                        created_facilities += 1
                        if i <= 10:
                            print(f"  Created missing facility: {location_name}")
                    except Exception as e:
                        print(f"ERROR: Failed to create facility {location_name}: {e}")
                        session.rollback()
                        continue
                
                if not facility:
                    not_found_count += 1
                    if i <= 10:  # Show first few misses for debugging
                        print(f"  Location ID not found and cannot create: {location_id}")
                    continue
                
                # Enrich facility with location data
                facility.registered_manager = row.get('Registered manager', '').strip()
                facility.location_uprn = row.get('Location UPRN', '').strip()
                facility.location_telephone = row.get('Location telephone number', '').strip()
                facility.location_web_address = row.get('Location Web Address', '').strip()
                facility.primary_inspection_category = row.get('Primary inspection category', '').strip()
                facility.care_home_beds = parse_bed_count(row.get('Care homes beds', ''))
                facility.location_start_date = row.get('Location HSCA start date', '').strip()
                facility.location_end_date = row.get('Location HSCA end date', '').strip()
                facility.dormant = row.get('Dormant', '').strip()
                facility.latest_overall_rating = row.get('Location Latest Overall Rating', '').strip()
                facility.publication_date = row.get('Publication Date', '').strip()
                facility.service_users_supported = row.get('Service users supported', '').strip()
                facility.care_home_size_band = row.get('Size of care home (bands by number of beds)', '').strip()
                facility.location_length_service_band = row.get('Location length of service (bands by number of years)', '').strip()
                facility.safe_rating = row.get('Location safe rating', '').strip()
                facility.effective_rating = row.get('Location effective rating', '').strip()
                facility.caring_rating = row.get('Location caring rating', '').strip()
                facility.responsive_rating = row.get('Location responsive rating', '').strip()
                facility.well_led_rating = row.get('Location well-led rating', '').strip()
                
                enriched_count += 1
                
                if i <= 5:  # Show first few enrichments
                    print(f"  Enriched: {facility.name} (Rating: {facility.latest_overall_rating})")
                
                # Progress reporting and commit every 5000 rows
                if i % 5000 == 0:
                    elapsed = time.time() - start_time
                    rate = i / elapsed
                    eta_seconds = (total_rows - i) / rate if rate > 0 else 0
                    eta_minutes = eta_seconds / 60
                    
                    print(f"\n--- Progress Report ---")
                    print(f"Processed: {i:,}/{total_rows:,} rows ({i/total_rows*100:.1f}%)")
                    print(f"Rate: {rate:.1f} rows/second")
                    print(f"ETA: {eta_minutes:.1f} minutes")
                    print(f"Facilities enriched: {enriched_count:,}")
                    print(f"Providers created: {created_providers:,}")
                    print(f"Facilities created: {created_facilities:,}")
                    print(f"Not found: {not_found_count:,}")
                    
                    try:
                        commit_start = time.time()
                        session.commit()
                        commit_time = time.time() - commit_start
                        batch_time = time.time() - batch_start_time
                        print(f"  Batch committed in {commit_time:.2f}s (total batch time: {batch_time:.2f}s)")
                        batch_start_time = time.time()
                    except Exception as e:
                        session.rollback()
                        print(f"ERROR: Failed to commit batch at row {i}: {e}")
                        sys.exit(1)
                
                # Quick progress dots every 1000 rows
                elif i % 1000 == 0:
                    print(".", end="", flush=True)
            
            # Final commit
            print(f"\n\nFinalizing enrichment...")
            try:
                session.commit()
                print("Final commit completed")
            except Exception as e:
                session.rollback()
                print(f"ERROR: Failed in final commit: {e}")
                sys.exit(1)
                
    except IOError as e:
        print(f"ERROR: Failed to read CSV file: {e}")
        sys.exit(1)
    
    total_time = time.time() - start_time
    print(f"\nEnrichment completed in {total_time/60:.1f} minutes")
    
    return enriched_count, created_providers, created_facilities, not_found_count

def main():
    if len(sys.argv) != 2:
        print("Usage: python enrich_locations.py <locations_csv_file>")
        sys.exit(1)
    
    csv_file = sys.argv[1]
    if not os.path.exists(csv_file):
        print(f"ERROR: File {csv_file} not found")
        sys.exit(1)
    
    print(f"=== Location Enrichment Started ===")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"File: {csv_file}")
    
    # Database setup
    database_url = os.getenv('DATABASE_URL', 'postgresql://darwinist:darwinist@localhost:5432/darwinist')
    print(f"Database: {database_url.split('@')[1] if '@' in database_url else 'local'}")
    
    engine = create_engine(database_url)
    Session = sessionmaker(bind=engine)
    session = Session()
    
    # Ensure database tables exist with new columns
    print("Ensuring database schema is up to date...")
    db.metadata.create_all(engine)
    
    total_rows = count_csv_rows(csv_file)
    print(f"Total location rows to process: {total_rows:,}")
    
    overall_start = time.time()
    
    try:
        enriched_count, created_providers, created_facilities, not_found_count = enrich_facilities(csv_file, session, total_rows)
        
        total_time = time.time() - overall_start
        
        print(f"\n=== Enrichment Summary ===")
        print(f"Total time: {total_time/60:.1f} minutes")
        print(f"Average rate: {total_rows/total_time:.1f} rows/second")
        print(f"Facilities enriched: {enriched_count:,}")
        print(f"Providers created: {created_providers:,}")
        print(f"Facilities created: {created_facilities:,}")
        print(f"Location IDs not found: {not_found_count:,}")
        print(f"Match rate: {(enriched_count/(enriched_count+not_found_count)*100):.1f}%")
        print(f"Completed at: {datetime.now().strftime('%H:%M:%S')}")
        
    finally:
        session.close()

if __name__ == '__main__':
    main()