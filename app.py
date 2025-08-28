import os
import csv
from flask import Flask, render_template, request, redirect, url_for, flash
from dotenv import load_dotenv
from model import db, Contact

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here'
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'postgresql://darwinist:darwinist@localhost:5431/darwinist')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)



@app.route('/')
def index():
    page = request.args.get('page', 1, type=int)
    search_term = request.args.get('search', '')
    per_page = 50
    
    query = Contact.query
    
    if search_term:
        search_filter = f"%{search_term}%"
        query = query.filter(
            db.or_(
                Contact.name.ilike(search_filter),
                Contact.town_city.ilike(search_filter),
                Contact.region.ilike(search_filter),
                Contact.service_types.ilike(search_filter),
                Contact.specialisms_services.ilike(search_filter),
                Contact.provider_name.ilike(search_filter)
            )
        )
    
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    contacts = pagination.items
    total_contacts = query.count()
    
    return render_template('index.html', 
                         contacts=contacts,
                         total_contacts=total_contacts,
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
            # Read CSV content
            content = file.read().decode('utf-8')
            csv_reader = csv.DictReader(content.splitlines())
            
            imported_count = 0
            for row in csv_reader:
                contact = Contact(
                    name=row.get('Name', '').strip(),
                    address_1=row.get('Address 1', '').strip(),
                    address_2=row.get('Address 2', '').strip(),
                    town_city=row.get('Town/City', '').strip(),
                    county=row.get('County', '').strip(),
                    postcode=row.get('Postcode', '').strip(),
                    phone_number=row.get('Phone number', '').strip(),
                    cqc_provider_id=row.get('CQC Provider ID (for office use only)', '').strip(),
                    cqc_location_id=row.get('CQC Location ID', '').strip(),
                    website=row.get('Website', '').strip(),
                    local_authority=row.get('Local authority', '').strip(),
                    region=row.get('Region', '').strip(),
                    report_publication_date=row.get('Report publication date', '').strip(),
                    url=row.get('URL', '').strip(),
                    also_known_as=row.get('Also known as', '').strip(),
                    specialisms_services=row.get('Specialisms/services', '').strip(),
                    service_types=row.get('Service types', '').strip(),
                    provider_name=row.get('Provider name', '').strip(),
                    email_address=''  # Empty for now, will be added later
                )
                db.session.add(contact)
                imported_count += 1
            
            db.session.commit()
            flash(f'Successfully imported {imported_count} contacts')
            
        except Exception as e:
            db.session.rollback()
            flash(f'Error importing CSV: {str(e)}')
    else:
        flash('Please select a valid CSV file')
    
    return redirect(url_for('index'))

def create_tables():
    with app.app_context():
        db.create_all()

if __name__ == '__main__':
    create_tables()
    app.run(debug=True, host='0.0.0.0', port=5000)