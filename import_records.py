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
    print("Counting rows in CSV file...")
    start_time = time.time()
    try:
        with open(csv_file, 'r', encoding='utf-8') as f:
            row_count = sum(1 for _ in f) - 1  # Subtract header row
        elapsed = time.time() - start_time
        print(f"Row counting completed in {elapsed:.2f} seconds")
        return row_count
    except IOError as e:
        print(f"Error reading file: {e}")
        sys.exit(1)

def load_all_providers(session):
    """Load all existing providers into memory cache"""
    print("Loading existing providers into cache...")
    start_time = time.time()
    providers = session.query(Provider).all()
    cache = {provider.name: provider.id for provider in providers}
    elapsed = time.time() - start_time
    print(f"Loaded {len(cache)} existing providers in {elapsed:.2f}s")
    return cache

def bulk_create_providers(session, new_providers):
    """Create multiple providers in a single operation"""
    if not new_providers:
        return {}
    
    print(f"Bulk creating {len(new_providers)} new providers...")
    start_time = time.time()
    
    provider_objects = []
    for name, data in new_providers.items():
        provider = Provider(
            name=name,
            cqc_provider_id=data['cqc_provider_id'],
            website=data['website']
        )
        provider_objects.append(provider)
    
    try:
        session.add_all(provider_objects)
        session.flush()
        
        # Update cache with new provider IDs
        provider_cache = {}
        for provider in provider_objects:
            provider_cache[provider.name] = provider.id
        
        elapsed = time.time() - start_time
        print(f"Created {len(provider_objects)} providers in {elapsed:.2f}s")
        return provider_cache
        
    except Exception as e:
        session.rollback()
        print(f"ERROR: Failed to bulk create providers: {e}")
        return {}

def create_facility(row, provider_id):
    return Facility(
        name=row.get('Name', '').strip(),
        address_1=row.get('Address 1', '').strip(),
        address_2=row.get('Address 2', '').strip(),
        town_city=row.get('Town/City', '').strip(),
        county=row.get('County', '').strip(),
        postcode=row.get('Postcode', '').strip(),
        phone_number=row.get('Phone number', '').strip(),
        cqc_location_id=row.get('CQC Location ID (for office use only)', '').strip(),
        website=row.get('Website', '').strip(),
        local_authority=row.get('Local authority', '').strip(),
        region=row.get('Region', '').strip(),
        report_publication_date=row.get('Report publication date', '').strip(),
        url=row.get('URL', '').strip(),
        also_known_as=row.get('Also known as', '').strip(),
        specialisms_services=row.get('Specialisms/services', '').strip(),
        service_types=row.get('Service types', '').strip(),
        email_address='',
        provider_id=provider_id
    )

def commit_batch(session, row_number, batch_start_time):
    try:
        commit_start = time.time()
        session.commit()
        commit_time = time.time() - commit_start
        batch_time = time.time() - batch_start_time
        print(f"  Batch committed in {commit_time:.2f}s (total batch time: {batch_time:.2f}s)")
    except Exception as e:
        session.rollback()
        print(f"ERROR: Failed to commit batch at row {row_number}: {e}")
        sys.exit(1)

def process_csv_file(csv_file, session, total_rows):
    # Load all existing providers into cache
    providers_cache = load_all_providers(session)
    
    imported_facilities = 0
    skipped_rows = 0
    start_time = time.time()
    batch_start_time = time.time()
    
    # First pass: collect all unique providers from CSV
    print("Analyzing CSV for new providers...")
    new_providers = {}
    
    try:
        with open(csv_file, 'r', encoding='utf-8') as f:
            csv_reader = csv.DictReader(f)
            for row in csv_reader:
                provider_name = row.get('Provider name', '').strip()
                if provider_name and provider_name not in providers_cache:
                    if provider_name not in new_providers:
                        new_providers[provider_name] = {
                            'cqc_provider_id': row.get('CQC Provider ID (for office use only)', '').strip() or None,
                            'website': row.get('Website', '').strip()
                        }
    except IOError as e:
        print(f"ERROR: Failed to read CSV file: {e}")
        sys.exit(1)
    
    # Bulk create new providers
    new_provider_cache = bulk_create_providers(session, new_providers)
    providers_cache.update(new_provider_cache)
    created_providers = len(new_provider_cache)
    
    print(f"Starting facility import at {datetime.now().strftime('%H:%M:%S')}")
    
    # Second pass: create facilities with bulk operations
    facilities_batch = []
    
    try:
        with open(csv_file, 'r', encoding='utf-8') as f:
            csv_reader = csv.DictReader(f)
            
            for i, row in enumerate(csv_reader, 1):
                provider_name = row.get('Provider name', '').strip()
                if not provider_name or provider_name not in providers_cache:
                    skipped_rows += 1
                    continue
                
                facility = create_facility(row, providers_cache[provider_name])
                facilities_batch.append(facility)
                imported_facilities += 1
                
                # Bulk insert every 5000 facilities
                if len(facilities_batch) >= 5000:
                    session.add_all(facilities_batch)
                    facilities_batch = []
                    
                    elapsed = time.time() - start_time
                    rate = i / elapsed
                    eta_seconds = (total_rows - i) / rate if rate > 0 else 0
                    eta_minutes = eta_seconds / 60
                    
                    print(f"\n--- Progress Report ---")
                    print(f"Processed: {i:,}/{total_rows:,} rows ({i/total_rows*100:.1f}%)")
                    print(f"Rate: {rate:.1f} rows/second")
                    print(f"ETA: {eta_minutes:.1f} minutes")
                    print(f"Facilities imported: {imported_facilities:,}")
                    print(f"Rows skipped: {skipped_rows:,}")
                    
                    commit_batch(session, i, batch_start_time)
                    batch_start_time = time.time()
                
                # Quick progress dots every 1000 rows
                elif i % 1000 == 0:
                    print(".", end="", flush=True)
            
            # Insert remaining facilities
            if facilities_batch:
                session.add_all(facilities_batch)
            
            # Final commit
            print(f"\n\nFinalizing import...")
            final_start = time.time()
            commit_batch(session, "final", final_start)
            
    except IOError as e:
        print(f"ERROR: Failed to read CSV file: {e}")
        sys.exit(1)
    
    total_time = time.time() - start_time
    print(f"\nProcessing completed in {total_time/60:.1f} minutes")
    
    return created_providers, imported_facilities, skipped_rows

def main():
    if len(sys.argv) != 2:
        print("Usage: python import_records.py <csv_file>")
        sys.exit(1)
    
    csv_file = sys.argv[1]
    if not os.path.exists(csv_file):
        print(f"ERROR: File {csv_file} not found")
        sys.exit(1)
    
    print(f"=== CRM Data Import Started ===")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"File: {csv_file}")
    
    # Database setup
    database_url = os.getenv('DATABASE_URL', 'postgresql://darwinist:darwinist@localhost:5432/darwinist')
    print(f"Database: {database_url.split('@')[1] if '@' in database_url else 'local'}")
    
    engine = create_engine(database_url)
    Session = sessionmaker(bind=engine)
    session = Session()
    
    # Create tables if they don't exist
    print("Ensuring database tables exist...")
    db.metadata.create_all(engine)
    
    total_rows = count_csv_rows(csv_file)
    print(f"Total rows to process: {total_rows:,}")
    
    overall_start = time.time()
    
    try:
        created_providers, imported_facilities, skipped_rows = process_csv_file(csv_file, session, total_rows)
        
        total_time = time.time() - overall_start
        
        print(f"\n=== Import Summary ===")
        print(f"Total time: {total_time/60:.1f} minutes")
        print(f"Average rate: {total_rows/total_time:.1f} rows/second")
        print(f"Providers created: {created_providers:,}")
        print(f"Facilities imported: {imported_facilities:,}")
        print(f"Rows skipped: {skipped_rows:,}")
        print(f"Success rate: {((total_rows-skipped_rows)/total_rows*100):.1f}%")
        print(f"Completed at: {datetime.now().strftime('%H:%M:%S')}")
        
    finally:
        session.close()

if __name__ == '__main__':
    main()