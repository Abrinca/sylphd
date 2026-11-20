"""Compare stock sylph profile.tsv with sylphd's: hits, numeric differences, text differences.

usage: python -I compare.py [DIR]   DIR is run.sh's output directory (default: the current one)
"""
import csv,sys,collections,math,os
os.chdir(sys.argv[1] if len(sys.argv)>1 else '.')
def load(p):
    d=collections.defaultdict(dict)
    for r in csv.DictReader(open(p),delimiter='\t'):
        s=r['Sample_file'].split('/')[-1].replace('.paired','').replace('.sylsp','')
        d[s][r['Genome_file']]=r
    return d
num=['Taxonomic_abundance','Sequence_abundance','Adjusted_ANI','True_cov','Naive_ANI','Median_cov','Mean_cov_geq1','kmers_reassigned']
txt=['Containment_ind','Contig_name','Eff_lambda']
ref=load('sylph/profile.tsv')
for name in ['sylphd','sylphd_on_sylph_sk']:
    o=load(f'{name}/profile.tsv'); print('##',name,'vs stock sylph')
    for s in sorted(ref):
        a,b=ref[s],o.get(s,{})
        only_a=set(a)-set(b); only_b=set(b)-set(a); common=set(a)&set(b)
        maxd=collections.defaultdict(float); txtdiff=collections.Counter()
        for g in common:
            for c in num:
                x,y=a[g][c],b[g][c]
                if x in('','NA') and y in ('','NA'): continue
                maxd[c]=max(maxd[c],abs(float(x)-float(y)))
            for c in txt:
                x,y=a[g][c],b[g][c]
                if x.replace('NA','')!=y.replace('NA',''): txtdiff[c]+=1
        print(f'  {s:11} hits sylph={len(a)} {name}={len(b)} only_sylph={len(only_a)} only_sylphd={len(only_b)} | max|diff| '+', '.join(f'{c}={v:.4g}' for c,v in maxd.items() if v)+ (f' | text diffs {dict(txtdiff)}' if txtdiff else '') )
        for g in sorted(only_a)[:3]: print('     only sylph:',g,a[g]['Adjusted_ANI'],a[g]['Sequence_abundance'])
        for g in sorted(only_b)[:3]: print('     only sylphd:',g,b[g]['Adjusted_ANI'],b[g]['Sequence_abundance'])
