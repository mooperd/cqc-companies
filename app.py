import os
import csv
from flask import Flask, render_template, request, redirect, url_for, flash
from model import db, Provider, Facility, Contact

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'postgresql://darwinist:darwinist@localhost:5432/darwinist')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

@app.route('/healthz')
def healthz():
    return 'OK', 200

@app.route('/')
def index():
    page = request.args.get('page', 1, type=int)
    search_term = request.args.get('search', '')
    per_page = 50
    
    query = Facility.query
    
    if search_term:
        search_filter = f"%{search_term}%"
        query = query.join(Provider).filter(
            db.or_(
                Facility.name.ilike(search_filter),
                Facility.town_city.ilike(search_filter),
                Facility.region.ilike(search_filter),
                Facility.service_types.ilike(search_filter),
                Facility.specialisms_services.ilike(search_filter),
                Provider.name.ilike(search_filter)
            )
        )
    
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    facilities = pagination.items
    total_facilities = query.count()
    
    return render_template('index.html', 
                         facilities=facilities,
                         total_facilities=total_facilities,
                         page=page,
                         total_pages=pagination.pages,
                         search_term=search_term)

@app.route('/import', methods=['POST'])
def import_csv():
    if 'csv_file' not in request.files:
        flash('No file selected')
        return redirect(url_for('index'))
    
    file = request.files['csv_file']
    if file.filename == '':
        flash('No file selected')
        return redirect(url_for('index'))
    
    if file and file.filename.endswith('.csv'):
        try:
            content = file.read().decode('utf-8')
            csv_reader = csv.DictReader(content.splitlines())
            
            imported_count = 0
            for row in csv_reader:
                provider_name = row.get('Provider name', '').strip()
                cqc_provider_id = row.get('CQC Provider ID (for office use only)', '').strip()
                
                if not provider_name:
                    continue
                
                # Find or create provider
                provider = Provider.query.filter_by(name=provider_name).first()
                if not provider:
                    provider = Provider(
                        name=provider_name,
                        cqc_provider_id=cqc_provider_id,
                        website=row.get('Website', '').strip()
                    )
                    db.session.add(provider)
                    db.session.flush()  # Get the provider ID
                
                # Create facility
                facility = Facility(
                    name=row.get('Name', '').strip(),
                    address_1=row.get('Address 1', '').strip(),
                    address_2=row.get('Address 2', '').strip(),
                    town_city=row.get('Town/City', '').strip(),
                    county=row.get('County', '').strip(),
                    postcode=row.get('Postcode', '').strip(),
                    phone_number=row.get('Phone number', '').strip(),
                    cqc_location_id=row.get('CQC Location ID', '').strip(),
                    website=row.get('Website', '').strip(),
                    local_authority=row.get('Local authority', '').strip(),
                    region=row.get('Region', '').strip(),
                    report_publication_date=row.get('Report publication date', '').strip(),
                    url=row.get('URL', '').strip(),
                    also_known_as=row.get('Also known as', '').strip(),
                    specialisms_services=row.get('Specialisms/services', '').strip(),
                    service_types=row.get('Service types', '').strip(),
                    email_address='',
                    provider_id=provider.id
                )
                db.session.add(facility)
                imported_count += 1
            
            db.session.commit()
            flash(f'Successfully imported {imported_count} facilities')
            
        except Exception as e:
            db.session.rollback()
            flash(f'Error importing CSV: {str(e)}')
    else:
        flash('Please select a valid CSV file')
    
    return redirect(url_for('index'))

@app.route('/providers')
def providers():
    page = request.args.get('page', 1, type=int)
    search_term = request.args.get('search', '')
    exclude_nhs = request.args.get('exclude_nhs', '') == 'on'
    sort_by = request.args.get('sort_by', 'name')
    specialisms_filter = request.args.get('specialisms', '')
    service_types_filter = request.args.get('service_types', '')
    per_page = 20
    
    # Base query with facilities joined for filtering
    query = Provider.query.join(Facility)
    
    # Search filter
    if search_term:
        search_filter = f"%{search_term}%"
        query = query.filter(Provider.name.ilike(search_filter))
    
    # Exclude NHS providers
    if exclude_nhs:
        query = query.filter(~Provider.name.ilike('%nhs%'))
    
    # Filter by specialisms
    if specialisms_filter:
        query = query.filter(Facility.specialisms_services.ilike(f'%{specialisms_filter}%'))
    
    # Filter by service types
    if service_types_filter:
        query = query.filter(Facility.service_types.ilike(f'%{service_types_filter}%'))
    
    # Remove duplicates from joins
    query = query.distinct()
    
    # Sorting - need to handle facility count differently to avoid duplicate joins
    if sort_by == 'facility_count':
        # Use subquery to count facilities and sort
        from sqlalchemy import func
        facility_counts = db.session.query(
            Provider.id,
            func.count(Facility.id).label('facility_count')
        ).outerjoin(Facility).group_by(Provider.id).subquery()
        
        # Reset query to avoid duplicate joins
        query = Provider.query
        
        # Apply filters again without joins
        if search_term:
            search_filter = f"%{search_term}%"
            query = query.filter(Provider.name.ilike(search_filter))
        
        if exclude_nhs:
            query = query.filter(~Provider.name.ilike('%nhs%'))
        
        # Apply facility-based filters using EXISTS
        if specialisms_filter:
            query = query.filter(
                Provider.facilities.any(Facility.specialisms_services.ilike(f'%{specialisms_filter}%'))
            )
        
        if service_types_filter:
            query = query.filter(
                Provider.facilities.any(Facility.service_types.ilike(f'%{service_types_filter}%'))
            )
        
        # Join with facility counts and sort
        query = query.join(facility_counts, Provider.id == facility_counts.c.id).order_by(facility_counts.c.facility_count.desc())
    else:
        # Default sort by name
        query = query.order_by(Provider.name)
    
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    providers = pagination.items
    total_providers = query.count()
    
    # Define filter options
    specialisms_options = [
        "Accommodation for persons who require nursing or personal care",
        "Caring for adults over 65 yrs",
        "Caring for adults under 65 yrs",
        "Caring for children (0 - 18yrs)",
        "Caring for people whose rights are restricted under the Mental Health Act",
        "Dementia",
        "Diagnostic and screening procedures",
        "Eating disorders",
        "Family planning services",
        "Learning disabilities",
        "Maternity and midwifery services",
        "Mental health conditions",
        "Personal care",
        "Physical disabilities",
        "Sensory impairments",
        "Services for everyone",
        "Substance misuse problems",
        "Surgical procedures",
        "Transport services, triage and medical advice provided remotely",
        "Treatment of disease, disorder or injury"
    ]
    
    service_types_options = [
        "Ambulances",
        "Clinic",
        "Community services - Healthcare",
        "Community services - Learning disabilities",
        "Community services - Mental Health",
        "Community services - Nursing",
        "Community services - Substance abuse",
        "Dentist",
        "Diagnosis/screening",
        "Doctors/GPs",
        "Home hospice care",
        "Homecare agencies",
        "Hospital",
        "Hospitals - Mental health/capacity",
        "Hospice",
        "Long-term conditions",
        "Mobile doctors",
        "NHS Body",
        "Nursing homes",
        "Organisation",
        "Partnership",
        "Phone/online advice",
        "Prison healthcare",
        "Rehabilitation (illness/injury)",
        "Rehabilitation (substance abuse)",
        "Residential homes",
        "Shared lives",
        "Supported housing",
        "Supported living",
        "Urgent care centres"
    ]
    
    return render_template('providers.html', 
                         providers=providers,
                         total_providers=total_providers,
                         page=page,
                         total_pages=pagination.pages,
                         search_term=search_term,
                         exclude_nhs=exclude_nhs,
                         sort_by=sort_by,
                         specialisms_filter=specialisms_filter,
                         service_types_filter=service_types_filter,
                         specialisms_options=specialisms_options,
                         service_types_options=service_types_options)



def create_tables():
    with app.app_context():
        db.create_all()

if __name__ == '__main__':
    create_tables()
    app.run(debug=True, host='0.0.0.0', port=5000)