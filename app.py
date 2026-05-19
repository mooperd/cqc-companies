import os
import csv
import io
import base64
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, flash
from model import db, Provider, Facility, Contact
import matplotlib
matplotlib.use('Agg')  # Use non-interactive backend
import matplotlib.pyplot as plt
import numpy as np
from sqlalchemy import func, case

load_dotenv()

app = Flask(__name__)
# Local-dev fallback only; production must supply SECRET_KEY via the env
# (Kubernetes Secret in k8s/secret.yaml, populated from secrets.FLASK_SECRET_KEY in CI).
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'your-secret-key-here')
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

@app.route('/statistics')
def statistics():
    # Set matplotlib style
    plt.style.use('default')
    
    # Generate all statistics and charts
    stats_data = generate_statistics()
    charts = generate_charts()
    
    return render_template('statistics.html', 
                         stats=stats_data,
                         charts=charts)

def generate_statistics():
    """Generate comprehensive statistics about the data"""
    stats = {}
    
    # Basic counts
    stats['total_providers'] = Provider.query.count()
    stats['total_facilities'] = Facility.query.count()
    stats['active_facilities'] = Facility.query.filter(Facility.dormant != 'Y').count()
    stats['dormant_facilities'] = Facility.query.filter(Facility.dormant == 'Y').count()
    
    # Care home statistics
    care_homes = Facility.query.filter(Facility.care_home_beds.isnot(None)).filter(Facility.care_home_beds > 0)
    stats['total_care_homes'] = care_homes.count()
    stats['total_beds'] = db.session.query(func.sum(Facility.care_home_beds)).scalar() or 0
    stats['avg_beds_per_home'] = round(stats['total_beds'] / stats['total_care_homes'], 1) if stats['total_care_homes'] > 0 else 0
    
    # Rating statistics
    rated_facilities = Facility.query.filter(Facility.latest_overall_rating.isnot(None))
    stats['rated_facilities'] = rated_facilities.count()
    stats['unrated_facilities'] = stats['total_facilities'] - stats['rated_facilities']
    
    # Rating breakdown
    rating_counts = db.session.query(
        Facility.latest_overall_rating,
        func.count(Facility.id)
    ).filter(Facility.latest_overall_rating.isnot(None)).group_by(Facility.latest_overall_rating).all()
    
    stats['rating_breakdown'] = {rating: count for rating, count in rating_counts}
    
    # Regional statistics
    regional_stats = db.session.query(
        Facility.region,
        func.count(Facility.id).label('facility_count'),
        func.sum(case((Facility.care_home_beds.isnot(None), Facility.care_home_beds), else_=0)).label('total_beds')
    ).filter(Facility.region.isnot(None)).group_by(Facility.region).order_by(func.count(Facility.id).desc()).all()
    
    stats['regional_stats'] = [
        {
            'region': region,
            'facilities': count,
            'beds': int(beds) if beds else 0
        }
        for region, count, beds in regional_stats
    ]
    
    # Service type statistics
    service_type_stats = {}
    all_facilities = Facility.query.filter(Facility.service_types.isnot(None)).all()
    for facility in all_facilities:
        if facility.service_types:
            types = [t.strip() for t in facility.service_types.split(',')]
            for service_type in types:
                if service_type:
                    service_type_stats[service_type] = service_type_stats.get(service_type, 0) + 1
    
    stats['top_service_types'] = sorted(service_type_stats.items(), key=lambda x: x[1], reverse=True)[:10]
    
    # Provider size distribution
    provider_sizes = db.session.query(
        func.count(Facility.id).label('facility_count')
    ).join(Provider).group_by(Provider.id).all()
    
    size_distribution = {}
    for (count,) in provider_sizes:
        if count == 1:
            size_distribution['1 facility'] = size_distribution.get('1 facility', 0) + 1
        elif count <= 5:
            size_distribution['2-5 facilities'] = size_distribution.get('2-5 facilities', 0) + 1
        elif count <= 10:
            size_distribution['6-10 facilities'] = size_distribution.get('6-10 facilities', 0) + 1
        elif count <= 20:
            size_distribution['11-20 facilities'] = size_distribution.get('11-20 facilities', 0) + 1
        else:
            size_distribution['20+ facilities'] = size_distribution.get('20+ facilities', 0) + 1
    
    stats['provider_size_distribution'] = size_distribution
    
    return stats

def generate_charts():
    """Generate all charts as base64 encoded images"""
    charts = {}
    
    # Chart 1: Rating Distribution Pie Chart
    charts['rating_pie'] = create_rating_pie_chart()
    
    # Chart 2: Regional Facilities Bar Chart
    charts['regional_bar'] = create_regional_bar_chart()
    
    # Chart 3: Care Home Size Distribution
    charts['bed_distribution'] = create_bed_distribution_chart()
    
    # Chart 4: Service Types Bar Chart
    charts['service_types'] = create_service_types_chart()
    
    # Chart 5: Provider Size Distribution
    charts['provider_sizes'] = create_provider_size_chart()
    
    # Chart 6: Facilities vs Beds by Region
    charts['regional_comparison'] = create_regional_comparison_chart()
    
    return charts

def create_rating_pie_chart():
    """Create pie chart for rating distribution"""
    rating_counts = db.session.query(
        Facility.latest_overall_rating,
        func.count(Facility.id)
    ).filter(Facility.latest_overall_rating.isnot(None)).group_by(Facility.latest_overall_rating).all()
    
    if not rating_counts:
        return None
    
    labels = []
    sizes = []
    colors = []
    
    color_map = {
        'Outstanding': '#28a745',
        'Good': '#007bff', 
        'Requires improvement': '#ffc107',
        'Inadequate': '#dc3545'
    }
    
    for rating, count in rating_counts:
        labels.append(f'{rating}\n({count})')
        sizes.append(count)
        colors.append(color_map.get(rating, '#6c757d'))
    
    fig, ax = plt.subplots(figsize=(10, 8))
    wedges, texts, autotexts = ax.pie(sizes, labels=labels, colors=colors, autopct='%1.1f%%', startangle=90)
    ax.set_title('CQC Rating Distribution', fontsize=16, fontweight='bold', pad=20)
    
    # Improve text readability
    for autotext in autotexts:
        autotext.set_color('white')
        autotext.set_fontweight('bold')
    
    plt.tight_layout()
    return fig_to_base64(fig)

def create_regional_bar_chart():
    """Create bar chart for facilities by region"""
    regional_stats = db.session.query(
        Facility.region,
        func.count(Facility.id)
    ).filter(Facility.region.isnot(None)).group_by(Facility.region).order_by(func.count(Facility.id).desc()).limit(15).all()
    
    if not regional_stats:
        return None
    
    regions = [r[0] for r in regional_stats]
    counts = [r[1] for r in regional_stats]
    
    fig, ax = plt.subplots(figsize=(12, 8))
    colors = plt.cm.Set3(np.linspace(0, 1, len(regions)))
    bars = ax.bar(range(len(regions)), counts, color=colors)
    
    ax.set_xlabel('Region', fontsize=12, fontweight='bold')
    ax.set_ylabel('Number of Facilities', fontsize=12, fontweight='bold')
    ax.set_title('Facilities by Region (Top 15)', fontsize=16, fontweight='bold', pad=20)
    ax.set_xticks(range(len(regions)))
    ax.set_xticklabels(regions, rotation=45, ha='right')
    
    # Add value labels on bars
    for bar, count in zip(bars, counts):
        height = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2., height + 0.5,
                f'{count}', ha='center', va='bottom', fontweight='bold')
    
    plt.tight_layout()
    return fig_to_base64(fig)

def create_bed_distribution_chart():
    """Create histogram for care home bed distribution"""
    beds_data = db.session.query(Facility.care_home_beds).filter(
        Facility.care_home_beds.isnot(None),
        Facility.care_home_beds > 0
    ).all()
    
    if not beds_data:
        return None
    
    beds = [b[0] for b in beds_data]
    
    fig, ax = plt.subplots(figsize=(12, 8))
    n, bins, patches = ax.hist(beds, bins=20, edgecolor='black', alpha=0.7, color='skyblue')
    
    ax.set_xlabel('Number of Beds', fontsize=12, fontweight='bold')
    ax.set_ylabel('Number of Care Homes', fontsize=12, fontweight='bold')
    ax.set_title('Care Home Size Distribution', fontsize=16, fontweight='bold', pad=20)
    
    # Add statistics text
    mean_beds = np.mean(beds)
    median_beds = np.median(beds)
    ax.axvline(mean_beds, color='red', linestyle='--', linewidth=2, label=f'Mean: {mean_beds:.1f}')
    ax.axvline(median_beds, color='green', linestyle='--', linewidth=2, label=f'Median: {median_beds:.1f}')
    ax.legend()
    
    plt.tight_layout()
    return fig_to_base64(fig)

def create_service_types_chart():
    """Create horizontal bar chart for top service types"""
    service_type_stats = {}
    all_facilities = Facility.query.filter(Facility.service_types.isnot(None)).all()
    
    for facility in all_facilities:
        if facility.service_types:
            types = [t.strip() for t in facility.service_types.split(',')]
            for service_type in types:
                if service_type:
                    service_type_stats[service_type] = service_type_stats.get(service_type, 0) + 1
    
    if not service_type_stats:
        return None
    
    # Get top 10 service types
    top_services = sorted(service_type_stats.items(), key=lambda x: x[1], reverse=True)[:10]
    services = [s[0] for s in top_services]
    counts = [s[1] for s in top_services]
    
    fig, ax = plt.subplots(figsize=(12, 8))
    colors = plt.cm.viridis(np.linspace(0, 1, len(services)))
    bars = ax.barh(range(len(services)), counts, color=colors)
    
    ax.set_yticks(range(len(services)))
    ax.set_yticklabels(services)
    ax.set_xlabel('Number of Facilities', fontsize=12, fontweight='bold')
    ax.set_title('Top 10 Service Types', fontsize=16, fontweight='bold', pad=20)
    
    # Add value labels
    for bar, count in zip(bars, counts):
        width = bar.get_width()
        ax.text(width + 1, bar.get_y() + bar.get_height()/2.,
                f'{count}', ha='left', va='center', fontweight='bold')
    
    plt.tight_layout()
    return fig_to_base64(fig)

def create_provider_size_chart():
    """Create pie chart for provider size distribution"""
    provider_sizes = db.session.query(
        func.count(Facility.id).label('facility_count')
    ).join(Provider).group_by(Provider.id).all()
    
    if not provider_sizes:
        return None
    
    size_distribution = {}
    for (count,) in provider_sizes:
        if count == 1:
            size_distribution['1 facility'] = size_distribution.get('1 facility', 0) + 1
        elif count <= 5:
            size_distribution['2-5 facilities'] = size_distribution.get('2-5 facilities', 0) + 1
        elif count <= 10:
            size_distribution['6-10 facilities'] = size_distribution.get('6-10 facilities', 0) + 1
        elif count <= 20:
            size_distribution['11-20 facilities'] = size_distribution.get('11-20 facilities', 0) + 1
        else:
            size_distribution['20+ facilities'] = size_distribution.get('20+ facilities', 0) + 1
    
    labels = list(size_distribution.keys())
    sizes = list(size_distribution.values())
    
    fig, ax = plt.subplots(figsize=(10, 8))
    wedges, texts, autotexts = ax.pie(sizes, labels=labels, autopct='%1.1f%%', startangle=90)
    ax.set_title('Provider Size Distribution', fontsize=16, fontweight='bold', pad=20)
    
    for autotext in autotexts:
        autotext.set_color('white')
        autotext.set_fontweight('bold')
    
    plt.tight_layout()
    return fig_to_base64(fig)

def create_regional_comparison_chart():
    """Create dual-axis chart comparing facilities and beds by region"""
    regional_stats = db.session.query(
        Facility.region,
        func.count(Facility.id).label('facility_count'),
        func.sum(case((Facility.care_home_beds.isnot(None), Facility.care_home_beds), else_=0)).label('total_beds')
    ).filter(Facility.region.isnot(None)).group_by(Facility.region).order_by(func.count(Facility.id).desc()).limit(10).all()
    
    if not regional_stats:
        return None
    
    regions = [r[0] for r in regional_stats]
    facilities = [r[1] for r in regional_stats]
    beds = [int(r[2]) if r[2] else 0 for r in regional_stats]
    
    fig, ax1 = plt.subplots(figsize=(14, 8))
    
    # Facilities bar chart
    color1 = 'tab:blue'
    ax1.set_xlabel('Region', fontsize=12, fontweight='bold')
    ax1.set_ylabel('Number of Facilities', color=color1, fontsize=12, fontweight='bold')
    bars1 = ax1.bar([r + ' ' for r in regions], facilities, color=color1, alpha=0.7, label='Facilities')
    ax1.tick_params(axis='y', labelcolor=color1)
    ax1.set_xticklabels(regions, rotation=45, ha='right')
    
    # Beds line chart
    ax2 = ax1.twinx()
    color2 = 'tab:red'
    ax2.set_ylabel('Total Beds', color=color2, fontsize=12, fontweight='bold')
    line = ax2.plot(range(len(regions)), beds, color=color2, marker='o', linewidth=3, markersize=8, label='Total Beds')
    ax2.tick_params(axis='y', labelcolor=color2)
    
    # Title and legend
    ax1.set_title('Facilities vs Total Beds by Region (Top 10)', fontsize=16, fontweight='bold', pad=20)
    
    # Combined legend
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc='upper right')
    
    plt.tight_layout()
    return fig_to_base64(fig)

def fig_to_base64(fig):
    """Convert matplotlib figure to base64 string"""
    img_buffer = io.BytesIO()
    fig.savefig(img_buffer, format='png', dpi=150, bbox_inches='tight')
    img_buffer.seek(0)
    img_str = base64.b64encode(img_buffer.getvalue()).decode()
    plt.close(fig)  # Important: close figure to free memory
    return img_str


def create_tables():
    with app.app_context():
        db.create_all()

if __name__ == '__main__':
    create_tables()
    app.run(debug=True, host='0.0.0.0', port=5000)